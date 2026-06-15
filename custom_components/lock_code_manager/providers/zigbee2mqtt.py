"""Module for Zigbee2MQTT locks."""

from __future__ import annotations

import asyncio
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
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

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

# Default Zigbee2MQTT base topic
DEFAULT_BASE_TOPIC = "zigbee2mqtt"

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


@dataclass(repr=False, eq=False)
class Zigbee2MQTTLock(BaseLock):
    """Class to represent Zigbee2MQTT lock."""

    _base_topic: str = field(init=False, default=DEFAULT_BASE_TOPIC)
    _friendly_name: str | None = field(init=False, default=None)
    _pending_codes: dict[int, asyncio.Future[str | None]] = field(
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
        """Subscribe to the device topic before the coordinator runs its first poll."""
        await self._async_ensure_device_subscription()

    def _get_friendly_name(self) -> str | None:
        """
        Get the Zigbee2MQTT friendly name for this device.

        Reads ``device_registry`` name on each call so renames stay aligned with the
        Zigbee2MQTT friendly name (cached value alone would go stale).
        """
        if not self.device_entry:
            LOGGER.debug("No device entry for %s", self.lock.entity_id)
            return None

        # Check if this is a Zigbee2MQTT device by identifiers
        is_z2m = any(
            len(identifier) >= 2 and str(identifier[1]).startswith("zigbee2mqtt_")
            for identifier in self.device_entry.identifiers
        )

        if not is_z2m:
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

    def _get_topic(self, suffix: str = "") -> str | None:
        """Get the MQTT topic for this device."""
        friendly_name = self._get_friendly_name()
        if not friendly_name:
            return None
        if suffix:
            return f"{self._base_topic}/{friendly_name}/{suffix}"
        return f"{self._base_topic}/{friendly_name}"

    def _maybe_raise_wrong_bridge_disconnect(self) -> None:
        """Raise when MQTT works but this entity cannot map to a Zigbee2MQTT topic."""
        if self.device_entry is None:
            return
        if any(
            len(identifier) >= 2 and str(identifier[1]).startswith("zigbee2mqtt_")
            for identifier in self.device_entry.identifiers
        ):
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

        # Handle users data in state update
        users_data = payload.get("users")
        if users_data and isinstance(users_data, dict):
            updates: dict[int, SlotCredential] = {}
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

                status = user_info.get("status")
                pin_code_present = "pin_code" in user_info
                pin_raw = user_info.get("pin_code")

                # Zigbee2MQTT often omits pin_code when expose_pin is false (default on
                # several Yale models). Treating that as empty makes the coordinator think
                # the slot is cleared, so disabling the slot skips clear_usercode while the
                # lock still holds the PIN. Only treat as empty when MQTT exposes the field.
                if status == "enabled":
                    if _mqtt_payload_pin_has_code_value(pin_raw):
                        updates[user_id] = SlotCredential.known(str(pin_raw))
                    elif pin_code_present:
                        updates[user_id] = SlotCredential.empty()
                    else:
                        continue
                else:
                    updates[user_id] = SlotCredential.empty()

            if updates and self.coordinator:
                LOGGER.debug(
                    "Lock %s received push update for slots: %s",
                    self.lock.entity_id,
                    list(updates.keys()),
                )
                self.coordinator.push_update(updates)

        # Handle response to get request with pin_code data
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
                        future.set_result(str(pin_code))
                    else:
                        future.set_result(None)

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
        # the next. Parallel gathers plus per-slot timeouts used to raise and fail the
        # entire refresh, leaving coordinator.data empty — sync then skips every slot
        # (see SlotSyncManager._resolve_slot_state).
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
                slot_states[slot_num] = (
                    SlotCredential.known(result) if result else SlotCredential.empty()
                )
            finally:
                self._pending_codes.pop(slot_num, None)

        return [user_from_slot(slot, state) for slot, state in slot_states.items()]

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """Perform hard refresh and return all codes."""
        return await self.async_get_usercodes()
