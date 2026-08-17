"""Module for Zigbee2MQTT locks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import json
from typing import Any, Literal

from homeassistant.components.mqtt import (
    DOMAIN as MQTT_DOMAIN,
    async_publish,
    async_subscribe,
)
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from ..const import DOMAIN
from ..domain.credentials import (
    Credential,
    CredentialRef,
    User,
    WriteResult,
    user_from_slot,
)
from ..domain.exceptions import LockDisconnected, LockOperationFailed
from ..domain.models import SlotCredential
from ._base import BaseLock
from ._util import parse_slot_num
from .const import LOGGER

# Default Zigbee2MQTT base topic, used as a fallback until (or unless) the
# bridge-topic registry resolves a device's actual base topic.
DEFAULT_BASE_TOPIC = "zigbee2mqtt"

# Every Zigbee2MQTT bridge publishes a retained device list under its own
# base topic; subscribing with a wildcard lets one Home Assistant instance
# learn the base topic of every bridge it can see, however many there are.
_BRIDGE_DEVICES_WILDCARD_TOPIC = "+/bridge/devices"
_BRIDGE_REGISTRY_DATA_KEY = "z2m_bridge_registry"

# Zigbee2MQTT action values for lock/unlock events triggered by PIN entry.
# These come from the DoorLock cluster's OperatingEventNotification and
# ProgrammingEventNotification via zigbee-herdsman-converters.
_Z2M_LOCK_ACTIONS_LOCKED = frozenset(
    {
        "lock",
        "keypad_lock",
        "manual_lock",
        "rf_lock",
    }
)
_Z2M_LOCK_ACTIONS_UNLOCKED = frozenset(
    {
        "unlock",
        "keypad_unlock",
        "manual_unlock",
        "rf_unlock",
    }
)
_Z2M_LOCK_ACTIONS = _Z2M_LOCK_ACTIONS_LOCKED | _Z2M_LOCK_ACTIONS_UNLOCKED


def _mqtt_payload_pin_has_code_value(pin_raw: Any) -> bool:
    """
    Return True when MQTT exposes a usable PIN value (including numeric zero).

    Plain truthiness is unsafe: ``0`` is a valid digit and must not be treated as
    absent. Boolean JSON values are ignored because they are not PIN payloads.
    """
    if pin_raw is None:
        return False
    if isinstance(pin_raw, bool):
        return False
    if isinstance(pin_raw, str):
        return pin_raw.strip() != ""
    return str(pin_raw) != ""


def _project_z2m_user_state(user_info: dict[str, Any]) -> SlotCredential:
    """
    Project one Zigbee2MQTT ``users`` entry to a SlotCredential.

    The status vocabulary comes from zigbee-herdsman-converters'
    ``lockUserStatus`` map (available/enabled/disabled); statuses outside
    it are published as ``not_supported_<n>``. Mapping traps:

    - ``enabled`` without a usable PIN value is occupied-but-withheld
      (``expose_pin`` off hides the code entirely), so it projects to
      unreadable -- treating it as empty would make sync reprogram a slot
      that already holds the right code.
    - The one exception: an explicit ``pin_code: null`` on an enabled user
      means the broker exposes the field and the device reports no code,
      so that projects to empty.
    - Unrecognized statuses (``not_supported_*``) project to unreadable,
      not empty, for the same reprogramming-storm reason.
    """
    status = user_info.get("status")
    pin_raw = user_info.get("pin_code")
    if status == "enabled":
        if _mqtt_payload_pin_has_code_value(pin_raw):
            return SlotCredential.known(str(pin_raw))
        if "pin_code" in user_info:
            return SlotCredential.empty()
        return SlotCredential.unreadable()
    if status in ("available", "disabled"):
        return SlotCredential.empty()
    return SlotCredential.unreadable()


@dataclass
class _BridgeTopicRegistry:
    """
    Map Zigbee2MQTT IEEE addresses to their bridge's base topic.

    A Home Assistant instance may have more than one Zigbee2MQTT bridge,
    each publishing under its own base topic (e.g. ``zigbee2mqtt`` and
    ``zigbee2mqtt_outbuilding``). Every bridge publishes a retained device
    list under ``<base_topic>/bridge/devices``, so subscribing to the
    wildcard ``+/bridge/devices`` and indexing each entry's
    ``ieee_address`` lets every ``Zigbee2MQTTLock`` resolve its own
    bridge's base topic instead of assuming the single default one.

    Shared (one instance per Home Assistant instance, via
    ``_get_bridge_registry``) and refcounted across every
    ``Zigbee2MQTTLock`` so the wildcard subscription is set up once and
    torn down only once the last lock releases it.
    """

    hass: HomeAssistant
    _ieee_to_base_topic: dict[str, str] = field(default_factory=dict, init=False)
    _listeners: dict[str, list[Callable[[], None]]] = field(
        default_factory=dict, init=False
    )
    _unsub: Callable[[], None] | None = field(default=None, init=False)
    _refcount: int = field(default=0, init=False)
    # Multiple locks under one config entry acquire concurrently
    # (asyncio.gather in _async_setup_new_locks); without this, two
    # acquires could both observe ``_unsub is None`` before either
    # finishes awaiting ``async_subscribe``, double-subscribing and
    # leaking the first unsub callable.
    _acquire_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def _handle_message(self, msg: ReceiveMessage) -> None:
        """Hand a bridge/devices message to the event loop (may run off it)."""
        self.hass.add_job(self._process_message, msg.topic, msg.payload)

    @callback
    def _process_message(self, topic: str, payload: Any) -> None:
        """Index a bridge/devices payload's IEEE addresses under its base topic."""
        base_topic = topic.rsplit("/bridge/devices", 1)[0]
        try:
            devices = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(devices, list):
            return

        newly_resolved: list[Callable[[], None]] = []
        for device in devices:
            if not isinstance(device, dict):
                continue
            ieee_address = device.get("ieee_address")
            if not isinstance(ieee_address, str):
                continue
            if self._ieee_to_base_topic.get(ieee_address) == base_topic:
                continue
            self._ieee_to_base_topic[ieee_address] = base_topic
            newly_resolved.extend(self._listeners.pop(ieee_address, []))

        for listener in newly_resolved:
            listener()

    async def async_acquire(self) -> None:
        """Register interest in the registry, subscribing on first use."""
        async with self._acquire_lock:
            self._refcount += 1
            if self._unsub is None:
                self._unsub = await async_subscribe(
                    self.hass, _BRIDGE_DEVICES_WILDCARD_TOPIC, self._handle_message
                )

    @callback
    def async_release(self) -> None:
        """Release interest in the registry, unsubscribing once nothing else needs it."""
        self._refcount -= 1
        if self._refcount <= 0 and self._unsub is not None:
            self._unsub()
            self._unsub = None
            self._ieee_to_base_topic.clear()
            self._listeners.clear()

    def get_base_topic(self, ieee_address: str) -> str | None:
        """Return the resolved base topic for an IEEE address, if known."""
        return self._ieee_to_base_topic.get(ieee_address)

    @callback
    def async_add_resolved_listener(
        self, ieee_address: str, listener: Callable[[], None]
    ) -> None:
        """
        Call ``listener`` once ``ieee_address`` resolves to a base topic.

        Fires immediately if already resolved.
        """
        if ieee_address in self._ieee_to_base_topic:
            listener()
            return
        self._listeners.setdefault(ieee_address, []).append(listener)


def _get_bridge_registry(hass: HomeAssistant) -> _BridgeTopicRegistry:
    """Return the hass-wide bridge-topic registry, creating it on first use."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    registry = domain_data.get(_BRIDGE_REGISTRY_DATA_KEY)
    if registry is None:
        registry = domain_data[_BRIDGE_REGISTRY_DATA_KEY] = _BridgeTopicRegistry(hass)
    return registry


@dataclass(repr=False, eq=False)
class Zigbee2MQTTLock(BaseLock):
    """Class to represent Zigbee2MQTT lock."""

    _friendly_name: str | None = field(init=False, default=None)
    _registry: _BridgeTopicRegistry | None = field(init=False, default=None)
    _registry_acquired: bool = field(init=False, default=False)
    _pending_codes: dict[int, asyncio.Future[SlotCredential]] = field(
        init=False, default_factory=dict
    )
    # Last projected state per slot from the most recent users payload;
    # the delta gate in _process_z2m_device_payload compares against this
    # so full-cached-state republications don't repush stale entries.
    _last_users_states: dict[int, SlotCredential] = field(
        init=False, default_factory=dict
    )

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return MQTT_DOMAIN

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return True

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return True

    @property
    def usercode_scan_interval(self) -> timedelta:
        """
        Return scan interval for usercodes.

        With push updates, we only need polling as a fallback.
        """
        return timedelta(minutes=5)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return interval for hard refresh."""
        return timedelta(hours=1)

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """
        Acquire the bridge-topic registry, then subscribe to the device topic.

        The registry is acquired once per instance (guarded so repeat calls
        on reconnect stay idempotent) and a resolved-topic listener is
        registered for this device's IEEE address so a subscription made
        before the owning bridge's retained ``bridge/devices`` message
        arrives gets corrected once it does.
        """
        if not self._registry_acquired:
            self._registry = _get_bridge_registry(self.hass)
            await self._registry.async_acquire()
            self._registry_acquired = True
            if (ieee_address := self._get_ieee_address()) is not None:
                self._registry.async_add_resolved_listener(
                    ieee_address, self._async_resubscribe_on_topic_resolved
                )
        await self._async_ensure_device_subscription()

    async def async_unload(self, remove_permanently: bool) -> None:
        """Tear down push subscriptions, then release the shared bridge registry."""
        await super().async_unload(remove_permanently)
        if self._registry_acquired and self._registry is not None:
            self._registry.async_release()
            self._registry_acquired = False

    @callback
    def _async_resubscribe_on_topic_resolved(self) -> None:
        """
        Re-subscribe to the device topic once its bridge's base topic is known.

        Read/write topics (``_get_topic``) are resolved fresh on every call
        and self-correct automatically as the registry learns more, but the
        device-state subscription made in ``async_setup`` is cached at
        subscribe time -- if it ran before this device's bridge published
        its retained ``bridge/devices`` message, it stays pinned to the
        wrong (default) topic until nudged.
        """
        self._clear_push_unsubs()
        self.hass.async_create_task(
            self._async_ensure_device_subscription(),
            f"Re-subscribe Zigbee2MQTT device topic for {self.lock.entity_id}",
        )

    def _get_ieee_address(self) -> str | None:
        """Return this device's Zigbee2MQTT IEEE address, or None if not a Z2M device."""
        if not self.device_entry:
            return None
        for identifier in self.device_entry.identifiers:
            if len(identifier) >= 2 and str(identifier[1]).startswith("zigbee2mqtt_"):
                return str(identifier[1])[len("zigbee2mqtt_") :]
        return None

    def _get_friendly_name(self) -> str | None:
        """
        Get the Zigbee2MQTT friendly name for this device.

        Reads ``device_registry`` name on each call so renames stay aligned with the
        Zigbee2MQTT friendly name (cached value alone would go stale).
        """
        if not self.device_entry:
            LOGGER.debug("No device entry for %s", self.lock.entity_id)
            return None

        if self._get_ieee_address() is None:
            LOGGER.debug("Device %s is not a Zigbee2MQTT device", self.lock.entity_id)
            return None

        name = self.device_entry.name
        if name != self._friendly_name:
            self._friendly_name = name
            LOGGER.debug(
                "Zigbee2MQTT friendly name for %s: %s",
                self.lock.entity_id,
                name,
            )
        return name

    def _get_base_topic(self) -> str:
        """
        Return the Zigbee2MQTT base topic for this device's bridge.

        Resolved fresh on every call from the shared bridge-topic registry
        so a mapping learned after this lock was set up is picked up
        immediately, without needing a cache invalidation path. Falls back
        to ``DEFAULT_BASE_TOPIC`` when unresolved (e.g. registry not yet
        acquired, or this bridge hasn't published ``bridge/devices`` yet) --
        the same behavior as before multi-bridge support existed.
        """
        ieee_address = self._get_ieee_address()
        if ieee_address is not None and self._registry is not None:
            if (base_topic := self._registry.get_base_topic(ieee_address)) is not None:
                return base_topic
        return DEFAULT_BASE_TOPIC

    def _get_topic(self, suffix: str = "") -> str | None:
        """Get the MQTT topic for this device."""
        friendly_name = self._get_friendly_name()
        if not friendly_name:
            return None
        base_topic = self._get_base_topic()
        if suffix:
            return f"{base_topic}/{friendly_name}/{suffix}"
        return f"{base_topic}/{friendly_name}"

    def _maybe_raise_wrong_bridge_disconnect(self) -> None:
        """Raise when MQTT works but this entity cannot map to a Zigbee2MQTT topic."""
        if self.device_entry is None:
            return
        if self._get_ieee_address() is not None:
            return
        raise LockDisconnected(
            "This entity is not a Zigbee2MQTT lock (device registry lacks a "
            "zigbee2mqtt_* identifier)."
        )

    async def async_is_integration_connected(self) -> bool:
        """Return whether MQTT is usable and this lock maps to a Z2M device topic."""
        if not mqtt_config_entry_enabled(self.hass):
            return False

        return bool(self._get_friendly_name())

    async def async_is_device_available(self) -> bool:
        """Return whether the lock entity reports an operational state."""
        state = self.hass.states.get(self.lock.entity_id)
        return not (state is None or state.state == "unavailable")

    @callback
    def _process_z2m_device_payload(self, payload: dict[str, Any]) -> None:
        """Apply device-topic JSON on the Home Assistant event loop."""
        action = payload.get("action")

        # Handle lock/unlock actions with user identification (keypad PIN usage)
        if isinstance(action, str) and action in _Z2M_LOCK_ACTIONS:
            action_user = payload.get("action_user")
            if action_user is not None and not isinstance(action_user, bool):
                code_slot = parse_slot_num(action_user)
                if code_slot is None:
                    LOGGER.debug(
                        "Ignoring %s with non-numeric action_user %r for %s",
                        action,
                        action_user,
                        self.lock.entity_id,
                    )
                    return
                to_locked = action in _Z2M_LOCK_ACTIONS_LOCKED
                self.async_fire_code_slot_event(
                    code_slot=code_slot,
                    to_locked=to_locked,
                    action_text=action,
                    source_data=payload,
                )
            return

        # Handle pin_code added / deleted (Z2M action events, not the users object)
        if action in ("pin_code_added", "pin_code_deleted"):
            action_user = payload.get("action_user")
            if action_user is not None:
                LOGGER.debug(
                    "Lock %s received %s for user %s",
                    self.lock.entity_id,
                    action,
                    action_user,
                )
                if self.coordinator:
                    self.hass.async_create_task(
                        self.coordinator.async_request_refresh()
                    )
            return

        users_data = payload.get("users")
        if users_data and isinstance(users_data, dict):
            states: dict[int, SlotCredential] = {}
            for user_id_str, user_info in users_data.items():
                user_id = parse_slot_num(user_id_str)
                if user_id is None:
                    LOGGER.warning(
                        "Skipping non-numeric Zigbee2MQTT user key %r for %s",
                        user_id_str,
                        self.lock.entity_id,
                    )
                    continue

                if not isinstance(user_info, dict):
                    LOGGER.debug(
                        "Skipping unexpected user_info type %s for slot %s on %s",
                        type(user_info).__name__,
                        user_id_str,
                        self.lock.entity_id,
                    )
                    continue

                states[user_id] = _project_z2m_user_state(user_info)

            # The converter answers GetPinCode through the users object
            # (fz.lock_pin_code_response), not through a pin_code response
            # payload -- resolve the pending read here or every slot read
            # times out (issue #1335). At most one read is pending at a
            # time (async_get_users queries slots sequentially), so cached
            # entries for other slots cannot satisfy a future they don't
            # belong to.
            for user_id, state in states.items():
                if (
                    future := self._pending_codes.pop(user_id, None)
                ) is not None and not future.done():
                    future.set_result(state)

            # Zigbee2MQTT republishes its full cached state on every
            # attribute change, so most users payloads restate old entries
            # rather than report changes. Applying them verbatim lets a
            # stale cache entry overwrite the optimistic push from a write
            # that the device already accepted -- the slot flips back to
            # its pre-write state and sync reprograms it forever (issue
            # #1335). Gate on the previous payload so only entries that
            # actually changed reach the coordinator.
            #
            # The gate only records payloads once a coordinator is
            # attached: retained/live messages can arrive between
            # async_setup's subscription and coordinator attach, and a
            # pre-attach snapshot would gate out the first post-attach
            # republication that should seed the initial state.
            if self.coordinator is not None:
                changed = {
                    user_id: state
                    for user_id, state in states.items()
                    if self._last_users_states.get(user_id) != state
                }
                self._last_users_states.update(states)
                if changed:
                    LOGGER.debug(
                        "Lock %s received push update for slots: %s",
                        self.lock.entity_id,
                        list(changed),
                    )
                    for user_id, state in changed.items():
                        self._confirm_slot(user_id, state)

        pin_code_data = payload.get("pin_code")
        if pin_code_data and isinstance(pin_code_data, dict):
            raw_user = pin_code_data.get("user")
            if raw_user is None:
                LOGGER.debug(
                    "Ignoring pin_code payload without user field for %s",
                    self.lock.entity_id,
                )
                return

            user_id = parse_slot_num(raw_user)
            if user_id is None:
                LOGGER.warning(
                    "Ignoring pin_code payload with non-numeric user for %s",
                    self.lock.entity_id,
                )
                return

            if user_id in self._pending_codes:
                future = self._pending_codes.pop(user_id)
                if not future.done():
                    user_enabled = pin_code_data.get("user_enabled", False)
                    pin_code = pin_code_data.get("pin_code")
                    if user_enabled and _mqtt_payload_pin_has_code_value(pin_code):
                        future.set_result(SlotCredential.known(str(pin_code)))
                    else:
                        future.set_result(SlotCredential.empty())

    async def _async_ensure_device_subscription(self) -> None:
        """Subscribe to the Z2M device topic; idempotent."""
        if self._push_unsubs:
            return

        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        topic = self._get_topic()
        if not topic:
            raise LockDisconnected(
                f"Cannot subscribe for {self.lock.entity_id} — "
                "not a Zigbee2MQTT device or friendly name unavailable"
            )

        def message_received(msg: ReceiveMessage) -> None:
            """Handle incoming MQTT messages (may run off the event loop)."""
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, TypeError) as err:
                LOGGER.debug(
                    "Ignoring invalid MQTT JSON for %s: %s",
                    self.lock.entity_id,
                    err,
                )
                return

            self.hass.add_job(self._process_z2m_device_payload, payload)

        try:
            unsub = await async_subscribe(self.hass, topic, message_received)
        except HomeAssistantError as err:
            LOGGER.error(
                "Failed to subscribe to MQTT for %s: %s",
                self.lock.entity_id,
                err,
            )
            raise LockDisconnected(
                f"Failed to subscribe to MQTT for {self.lock.entity_id}"
            ) from err
        self._register_push_unsub(unsub)
        LOGGER.debug("Subscribed to MQTT topic %s for %s", topic, self.lock.entity_id)

    @callback
    def setup_push_subscription(self) -> None:
        """
        Subscribe via background task when still unsubscribed (e.g. reconnect).

        Primary subscribe is ``await`` in ``async_setup``.
        """
        if self._push_unsubs:
            return

        topic = self._get_topic()
        if not topic:
            LOGGER.debug(
                "Cannot subscribe to push updates for %s - no topic",
                self.lock.entity_id,
            )
            raise LockDisconnected(
                f"Cannot subscribe to push updates for {self.lock.entity_id} - no topic"
            )

        if not mqtt_config_entry_enabled(self.hass):
            LOGGER.debug(
                "Deferring MQTT push subscribe for %s — MQTT integration disabled",
                self.lock.entity_id,
            )
            return

        async def _subscribe_or_log() -> None:
            """
            Run ``_async_ensure_device_subscription`` from the reconnect task path.

            Log errors only; sync ``setup_push_subscription`` cannot raise.
            """
            try:
                await self._async_ensure_device_subscription()
            except LockDisconnected as err:
                LOGGER.debug(
                    "Lock %s: push subscription deferred (disconnected): %s",
                    self.lock.entity_id,
                    err,
                )
            except Exception:
                LOGGER.exception(
                    "Lock %s: MQTT subscribe failed unexpectedly",
                    self.lock.entity_id,
                )

        self.hass.async_create_task(_subscribe_or_log())

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from MQTT updates."""
        had_subscription = bool(self._push_unsubs)
        self._clear_push_unsubs()
        if had_subscription:
            LOGGER.debug("Unsubscribed from MQTT for %s", self.lock.entity_id)

        # Cancel any pending futures
        for future in self._pending_codes.values():
            if not future.done():
                future.cancel()
        self._pending_codes.clear()

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> WriteResult:
        """
        Set a Personal Identification Number credential on a code slot.

        Publishes a Zigbee2MQTT ``set`` payload and immediately pushes an
        optimistic coordinator update (MQTT QoS 0 gives no delivery
        guarantee; hard-refresh mitigates drift). ``user_id`` is ignored;
        slot-only providers address the credential by ``credential.slot``.
        """
        code_slot = credential.slot

        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        if not await self.async_is_integration_connected():
            self._maybe_raise_wrong_bridge_disconnect()
            raise LockDisconnected("Lock not connected")

        set_topic = self._get_topic("set")
        if not set_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        # Zigbee2MQTT set_pin_code payload format
        payload = json.dumps(
            {
                "pin_code": {
                    "user": code_slot,
                    "user_type": "unrestricted",
                    "pin_code": pin,
                    "user_enabled": True,
                }
            }
        )

        try:
            await async_publish(self.hass, set_topic, payload)
        except OSError as err:
            # Network-level publish failure (broker unreachable). Route to
            # disconnect so the reconnect path runs instead of breaking
            # per-slot.
            LOGGER.error(
                "Failed to set PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to set PIN: {err}") from err
        except HomeAssistantError as err:
            LOGGER.error(
                "Failed to set PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockOperationFailed(f"Failed to set PIN: {err}") from err

        LOGGER.debug(
            "Published set_pin_code for %s slot %s",
            self.lock.entity_id,
            code_slot,
        )
        # Optimistic coordinator update after publish (MQTT QoS 0); hard_refresh mitigates drift.
        self._push_credential_update(code_slot, SlotCredential.known(pin))
        return WriteResult.CONFIRMED

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Clear a Personal Identification Number from a code slot.

        Publishes a Zigbee2MQTT ``set`` payload with ``user_enabled=false``
        and ``pin_code=null`` (many locks require both to fully clear the
        slot) and immediately pushes an optimistic coordinator update.
        See ``async_set_credential`` for the OSError-versus-HomeAssistantError
        routing rationale.
        """
        code_slot = ref.slot

        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        if not await self.async_is_integration_connected():
            self._maybe_raise_wrong_bridge_disconnect()
            raise LockDisconnected("Lock not connected")

        set_topic = self._get_topic("set")
        if not set_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        # Z2M: many locks need user_enabled false and pin_code null to clear the slot
        # (user_enabled only is not always enough on the device).
        payload = json.dumps(
            {
                "pin_code": {
                    "user": code_slot,
                    "user_type": "unrestricted",
                    "user_enabled": False,
                    "pin_code": None,
                }
            }
        )

        try:
            await async_publish(self.hass, set_topic, payload)
        except OSError as err:
            # See ``async_set_credential`` for the OSError split rationale.
            LOGGER.error(
                "Failed to clear PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to clear PIN: {err}") from err
        except HomeAssistantError as err:
            LOGGER.error(
                "Failed to clear PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockOperationFailed(f"Failed to clear PIN: {err}") from err

        LOGGER.debug(
            "Published clear_pin_code for %s slot %s",
            self.lock.entity_id,
            code_slot,
        )
        # Same optimistic push as ``async_set_credential``.
        self._push_credential_update(code_slot, SlotCredential.empty())
        return True

    async def async_get_users(self) -> list[User]:
        """
        Read Personal Identification Number codes from all managed slots.

        Queries Zigbee2MQTT one slot at a time over MQTT so the bridge can
        respond to each GET before the next. Transient publish/timeout/read
        failures produce an unreadable credential so the coordinator does
        not treat a transient MQTT error as a confirmed-empty slot and storm
        reprogramming after recovery.
        """
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        if not await self.async_is_integration_connected():
            self._maybe_raise_wrong_bridge_disconnect()
            raise LockDisconnected("Lock not connected")

        if not await self.async_is_device_available():
            raise LockDisconnected("Device not available")

        get_topic = self._get_topic("get")
        if not get_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        # Get configured code slots for this lock (any LCM entry that includes this lock).
        code_slots = self.managed_slots

        if not code_slots:
            return []

        loop = asyncio.get_running_loop()
        slot_states: dict[int, SlotCredential] = {}

        # Query one slot at a time so Zigbee2MQTT / firmware can answer each GET before
        # the next. Parallel gather + per-slot timeouts can fail the entire refresh and
        # leave coordinator.data empty -- sync then skips every slot (see
        # SlotSyncManager._resolve_credential_snapshot).
        # Transient publish/timeout/read failures use the unreadable credential so sync
        # does not treat the slot as confirmed-empty and storm reprogramming after MQTT
        # recovery.
        for slot_num in sorted(code_slots):
            future = loop.create_future()
            self._pending_codes[slot_num] = future
            payload = json.dumps({"pin_code": {"user": slot_num}})
            try:
                await async_publish(self.hass, get_topic, payload)
            except (HomeAssistantError, OSError) as err:
                LOGGER.debug(
                    "MQTT publish failed for PIN get %s slot %s: %s",
                    self.lock.entity_id,
                    slot_num,
                    err,
                )
                slot_states[slot_num] = SlotCredential.unreadable()
                self._pending_codes.pop(slot_num, None)
                continue

            try:
                result = await asyncio.wait_for(future, timeout=10.0)
            except TimeoutError:
                LOGGER.debug(
                    "Timeout waiting for PIN code response for %s slot %s",
                    self.lock.entity_id,
                    slot_num,
                )
                slot_states[slot_num] = SlotCredential.unreadable()
            except Exception as err:
                # Broad catch is intentional: the future is resolved by the MQTT
                # callback, and any exception from resolution (InvalidStateError,
                # data processing errors) should not crash the entire refresh.
                # CancelledError is BaseException in Python 3.11+ and propagates.
                LOGGER.warning(
                    "Unexpected error getting PIN for %s slot %s: %s",
                    self.lock.entity_id,
                    slot_num,
                    err,
                )
                slot_states[slot_num] = SlotCredential.unreadable()
            else:
                slot_states[slot_num] = result
            finally:
                self._pending_codes.pop(slot_num, None)

        return [user_from_slot(slot, state) for slot, state in slot_states.items()]

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """Perform hard refresh and return all codes."""
        return await self.async_get_usercodes()
