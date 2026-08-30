"""Module for Zigbee2MQTT locks."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field
from functools import partial
import json
from typing import Any, Literal, NoReturn

from homeassistant.components.mqtt import (
    async_publish,
    async_subscribe,
)
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from ..domain.credentials import Credential, CredentialRef, User, WriteResult
from ..domain.exceptions import LockDisconnected, LockOperationFailed
from ..domain.models import SlotCredential
from ._mqtt import BaseMqttLock
from ._util import is_masked_code, parse_slot_num, resolve_discovery_payload
from .const import LOGGER

# Device registry identifier prefix Zigbee2MQTT uses in its HA discovery
# payloads; also consumed by providers.resolve_provider_class for dispatch.
Z2M_IDENTIFIER_PREFIX = "zigbee2mqtt_"

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
    - A lock that masks its codes publishes a usable-looking value that is
      all asterisks. That is the withheld state wearing a code's shape, so
      it lands in the same place -- see ``is_masked_code``.
    - ``available`` is the only status that means the slot holds nothing.
      ``disabled`` is a user the lock is refusing, and unrecognized statuses
      (``not_supported_*``) say nothing at all, so both project to
      unreadable for the same reprogramming-storm reason -- and so that
      allocation does not read either as a free credential index.
    """
    status = user_info.get("status")
    pin_raw = user_info.get("pin_code")
    if status == "enabled":
        if _mqtt_payload_pin_has_code_value(pin_raw):
            code = str(pin_raw)
            return (
                SlotCredential.unreadable()
                if is_masked_code(code)
                else SlotCredential.known(code)
            )
        if "pin_code" in user_info:
            return SlotCredential.empty()
        return SlotCredential.unreadable()
    if status == "available":
        return SlotCredential.empty()
    # ``disabled`` is a user the lock is holding and not accepting, not a
    # free slot. Reporting it empty tells allocation the index is available
    # and tells sync the slot is confirmed cleared.
    return SlotCredential.unreadable()


@dataclass(repr=False, eq=False)
class Zigbee2MQTTLock(BaseMqttLock):
    """Class to represent Zigbee2MQTT lock."""

    # `_async_read_slot` waits 10s per slot before giving up on that slot.
    _per_slot_read_budget: float = 10.0

    _pending_codes: dict[int, asyncio.Future[SlotCredential]] = field(
        init=False, default_factory=dict
    )
    # Last projected state per slot from the most recent users payload;
    # the delta gate in _process_z2m_device_payload compares against this
    # so full-cached-state republications don't repush stale entries.
    _last_users_states: dict[int, SlotCredential] = field(
        init=False, default_factory=dict
    )
    _subscribed_topic: str | None = field(init=False, default=None)

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return True

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return True

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Subscribe to the device topic before the coordinator runs its first poll."""
        await self._async_ensure_device_subscription()

    def _is_z2m_device(self) -> bool:
        """Return whether the device registry marks this as a Zigbee2MQTT device."""
        if not self.device_entry:
            return False
        return any(
            len(identifier) >= 2
            and str(identifier[1]).startswith(Z2M_IDENTIFIER_PREFIX)
            for identifier in self.device_entry.identifiers
        )

    def _resolve_device_topic(self) -> str | None:
        """
        Resolve this lock's Zigbee2MQTT device topic from its MQTT discovery data.

        The discovery payload Zigbee2MQTT publishes carries the exact topics
        (``state_topic`` is ``<base_topic>/<friendly_name>``), so custom,
        multi-level, and per-bridge base topics all work verbatim with no
        reconstruction. Returns None whenever the topic cannot be determined —
        callers must treat that as disconnected rather than guess a topic.
        """
        if not self._is_z2m_device():
            return None
        payload = resolve_discovery_payload(self.hass, self.lock)
        if payload is None:
            return None
        state_topic = payload.get("state_topic")
        if isinstance(state_topic, str) and state_topic:
            return state_topic
        # Zigbee2MQTT's command topic is the device topic plus ``/set``, so
        # stripping the suffix names the same device the state topic would.
        command_topic = payload.get("command_topic")
        if isinstance(command_topic, str) and command_topic.endswith("/set"):
            return command_topic.removesuffix("/set")
        LOGGER.debug(
            "Discovery payload for %s has no usable topic",
            self.lock.entity_id,
        )
        return None

    def _get_topic(self, suffix: str = "") -> str | None:
        """Get the MQTT topic for this device, resolved from discovery data."""
        device_topic = self._resolve_device_topic()
        if not device_topic:
            return None
        if suffix:
            return f"{device_topic}/{suffix}"
        return device_topic

    def _maybe_raise_wrong_bridge_disconnect(self) -> None:
        """Raise when MQTT works but this entity cannot map to a Zigbee2MQTT topic."""
        if self.device_entry is None:
            return
        if self._is_z2m_device():
            return
        raise LockDisconnected(
            "This entity is not a Zigbee2MQTT lock (device registry lacks a "
            "zigbee2mqtt_* identifier)."
        )

    def _raise_not_connected(self) -> NoReturn:
        """Name the wrong-bridge misconfiguration before the generic reason."""
        self._maybe_raise_wrong_bridge_disconnect()
        super()._raise_not_connected()

    async def async_is_integration_connected(self) -> bool:
        """Return whether MQTT is usable and this lock maps to a Z2M device topic."""
        if not mqtt_config_entry_enabled(self.hass):
            return False
        return bool(self._resolve_device_topic())

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
                    if _mqtt_payload_pin_has_code_value(pin_code):
                        # A code is plainly here. Whether the lock is
                        # currently accepting it, and whether what it sent is
                        # the digits or a mask standing in for them, decide
                        # only whether the value can be compared -- not
                        # whether the index is taken. A disabled user whose
                        # code is also masked is doubly incomparable and
                        # lands in the same place.
                        code = str(pin_code)
                        future.set_result(
                            SlotCredential.known(code)
                            if user_enabled and not is_masked_code(code)
                            else SlotCredential.unreadable()
                        )
                    elif user_enabled and "pin_code" not in pin_code_data:
                        # Enabled, and the code withheld rather than reported
                        # -- ``expose_pin`` off. The same state the users
                        # object reports, and the same answer it gives.
                        future.set_result(SlotCredential.unreadable())
                    else:
                        # Either the lock says nothing is enabled here, or it
                        # answered the code explicitly with nothing.
                        future.set_result(SlotCredential.empty())

    async def _async_ensure_device_subscription(self) -> None:
        """Subscribe to the Z2M device topic; idempotent and drift-aware."""
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        topic = self._get_topic()
        if not topic:
            if self._push_unsubs:
                # Resolution is transiently unavailable; keep the existing
                # subscription rather than tearing down a working one.
                return
            raise LockDisconnected(
                f"Cannot subscribe for {self.lock.entity_id} — "
                "device topic not resolvable from MQTT discovery data"
            )

        if self._push_unsubs and self._subscribed_topic == topic:
            return

        # Topic changed (rename / bridge migration) or first subscribe.
        self._clear_push_unsubs()
        self._subscribed_topic = None

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
        self._subscribed_topic = topic
        LOGGER.debug("Subscribed to MQTT topic %s for %s", topic, self.lock.entity_id)

    @callback
    def setup_push_subscription(self) -> None:
        """
        Subscribe via background task when still unsubscribed (e.g. reconnect).

        Primary subscribe is ``await`` in ``async_setup``.
        """
        self._schedule_push_subscription(
            self._get_topic(), self._async_ensure_device_subscription, "topic"
        )

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from MQTT updates."""
        had_subscription = bool(self._push_unsubs)
        self._clear_push_unsubs()
        self._subscribed_topic = None
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

        await self._async_ensure_operational(require_device=False)

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

        await self._async_ensure_operational(require_device=False)

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

    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """
        Read Personal Identification Number codes one index at a time.

        What sequencing the reads buys, and why a read that reached nothing
        raises rather than reporting a lock full of unreadable slots, is
        ``BaseMqttLock._async_read_slots``. A broker that has stopped
        carrying traffic gets that far because every gate below answers from
        Home Assistant's configuration rather than from the wire.
        """
        await self._async_ensure_operational()

        # Renames/bridge migrations with no disconnect self-heal here: the
        # hourly hard refresh lands on this read and re-checks that the push
        # subscription still matches the currently resolved topic.
        try:
            await self._async_ensure_device_subscription()
        except LockDisconnected as err:
            LOGGER.debug(
                "Lock %s: could not refresh push subscription before poll: %s",
                self.lock.entity_id,
                err,
            )

        get_topic = self._get_topic("get")
        if not get_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        # One request per index, so the caller's scope bounds the work. The
        # topic is resolved once for the whole read rather than per slot: it
        # comes from a discovery-data walk, and one answer for one poll is
        # also what keeps a mid-read rename from splitting it across topics.
        return await self._async_read_slots(
            self.managed_slots if slots is None else slots,
            partial(self._async_read_slot, get_topic=get_topic),
            transport_failure="failed to reach the lock",
        )

    async def _async_read_slot(
        self, slot_num: int, get_topic: str
    ) -> SlotCredential | None:
        """
        Ask the lock for one slot and wait for the answer; None means silence.

        A request that never left and a request nothing came back for are
        both silence: a slot the bridge described as withheld, disabled, or
        masked is data, and a slot that produced no reply at all is not.
        That is the distinction ``BaseMqttLock._async_read_slots`` needs to
        tell a poll from a read that reached nothing.
        """
        loop = asyncio.get_running_loop()
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
            self._pending_codes.pop(slot_num, None)
            return None

        try:
            result = await asyncio.wait_for(future, timeout=10.0)
        except TimeoutError:
            LOGGER.debug(
                "Timeout waiting for PIN code response for %s slot %s",
                self.lock.entity_id,
                slot_num,
            )
            credential = None
        except Exception as err:
            # Broad catch is intentional: the future is resolved by the MQTT
            # callback, and any exception from resolution (InvalidStateError,
            # data processing errors) should not crash the entire refresh.
            # CancelledError is BaseException in Python 3.11+ and propagates.
            # Only an arriving reply can resolve the future, so this is a
            # reply that could not be made sense of rather than silence: it
            # stays a credential and keeps the poll alive.
            LOGGER.warning(
                "Unexpected error getting PIN for %s slot %s: %s",
                self.lock.entity_id,
                slot_num,
                err,
            )
            credential = SlotCredential.unreadable()
        else:
            credential = result
        finally:
            self._pending_codes.pop(slot_num, None)
        return credential

    async def async_get_max_slot(self) -> int | None:
        """
        Report no opinion: the bridge is not asked.

        Zigbee2MQTT publishes device definitions on its bridge topic, and a
        lock's ``pin_code`` expose can carry the user range, but this
        provider subscribes only to the device's own topic. Reading the
        definition is worth doing -- this is a lock that answers one index
        per round trip, so the limit is what the search costs -- and wants a
        real bridge payload to work from rather than a guessed shape.
        """
        return None
