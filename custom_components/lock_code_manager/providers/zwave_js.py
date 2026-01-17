"""Module for Z-Wave JS locks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from zwave_js_server.client import Client
from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.lock import ATTR_CODE_SLOT, ATTR_USERCODE
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import get_usercode, get_usercodes

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.components.zwave_js.const import (
    ATTR_EVENT,
    ATTR_EVENT_LABEL,
    ATTR_HOME_ID,
    ATTR_NODE_ID,
    ATTR_PARAMETERS,
    ATTR_TYPE,
    DOMAIN as ZWAVE_JS_DOMAIN,
    SERVICE_CLEAR_LOCK_USERCODE,
    SERVICE_SET_LOCK_USERCODE,
    ZWAVE_JS_NOTIFICATION_EVENT,
)
from homeassistant.components.zwave_js.helpers import async_get_node_from_entity_id
from homeassistant.components.zwave_js.models import ZwaveJSData
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_PIN,
    STATE_ON,
)
from homeassistant.core import Event, callback
from homeassistant.helpers.event import async_call_later

from ..const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from ..data import get_entry_data
from ..exceptions import LockDisconnected
from ._base import BaseLock

_LOGGER = logging.getLogger(__name__)
PUSH_SUBSCRIBE_RETRY_DELAY = 10

# All known Access Control Notification CC events that indicate the lock is locked
# or unlocked
ACCESS_CONTROL_NOTIFICATION_TO_LOCKED = {
    True: (
        AccessControlNotificationEvent.AUTO_LOCK_LOCKED_OPERATION,
        AccessControlNotificationEvent.KEYPAD_LOCK_OPERATION,
        AccessControlNotificationEvent.LOCK_OPERATION_WITH_USER_CODE,
        AccessControlNotificationEvent.LOCKED_BY_RF_WITH_INVALID_USER_CODE,
        AccessControlNotificationEvent.MANUAL_LOCK_OPERATION,
        AccessControlNotificationEvent.RF_LOCK_OPERATION,
    ),
    False: (
        AccessControlNotificationEvent.KEYPAD_UNLOCK_OPERATION,
        AccessControlNotificationEvent.MANUAL_UNLOCK_OPERATION,
        AccessControlNotificationEvent.RF_UNLOCK_OPERATION,
        AccessControlNotificationEvent.UNLOCK_BY_RF_WITH_INVALID_USER_CODE,
        AccessControlNotificationEvent.UNLOCK_OPERATION_WITH_USER_CODE,
    ),
}


@dataclass(repr=False, eq=False)
class ZWaveJSLock(BaseLock):
    """Class to represent ZWave JS lock."""

    lock_config_entry: ConfigEntry = field(repr=False)
    _listeners: list[Callable[[], None]] = field(init=False, default_factory=list)
    _value_update_unsub: Callable[[], None] | None = field(init=False, default=None)
    _push_retry_cancel: Callable[[], None] | None = field(init=False, default=None)

    @property
    def node(self) -> Node:
        """Return ZWave JS node."""
        return async_get_node_from_entity_id(
            self.hass, self.lock.entity_id, self.ent_reg
        )

    @property
    def supports_push(self) -> bool:
        """
        Return whether this lock supports push-based updates.

        Z-Wave JS emits value update events when the cache changes, so we
        subscribe to those instead of polling.
        """
        return True

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return True

    @property
    def connection_check_interval(self) -> timedelta | None:
        """Z-Wave JS exposes config entry state changes, so skip polling."""
        return None

    def _get_client_state(self) -> tuple[bool, str]:
        """Return whether the Z-Wave JS client is ready and a retry reason."""
        if self.lock_config_entry.state != ConfigEntryState.LOADED:
            return False, "config entry not loaded"

        runtime_data: ZwaveJSData | None = getattr(
            self.lock_config_entry, "runtime_data", None
        )
        client: Client | None = (
            getattr(runtime_data, "client", None) if runtime_data else None
        )
        if not client:
            return False, "Z-Wave JS client not ready"

        if not client.connected:
            return False, "Z-Wave JS client not connected"

        if client.driver is None:
            return False, "Z-Wave JS driver not ready"

        return True, ""

    def _is_masked(self, value: str) -> bool:
        """Check if a usercode value is masked (all asterisks)."""
        return bool(value) and value == "*" * len(value)

    def _resolve_masked(self, code_slot: int) -> str | None:
        """Resolve a masked usercode to the expected PIN value.

        Some locks return masked values (all asterisks) instead of the actual PIN.
        This method looks up the expected PIN from LCM entities for managed slots.

        Args:
            code_slot: The code slot number

        Returns:
            The expected PIN if slot is managed, active, and has valid PIN
            None if resolution fails (slot not in LCM, entities unavailable, etc.)

        """
        # Find config entry managing this lock and slot
        try:
            config_entry = next(
                entry
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
                and code_slot in (int(s) for s in get_entry_data(entry, CONF_SLOTS, {}))
            )
        except StopIteration:
            # Slot doesn't exist in LCM configuration
            return None

        # Look up entities
        base_unique_id = f"{config_entry.entry_id}|{code_slot}"
        active_entity_id = self.ent_reg.async_get_entity_id(
            SWITCH_DOMAIN, DOMAIN, f"{base_unique_id}|{CONF_ENABLED}"
        )
        pin_entity_id = self.ent_reg.async_get_entity_id(
            TEXT_DOMAIN, DOMAIN, f"{base_unique_id}|{CONF_PIN}"
        )

        if not active_entity_id or not pin_entity_id:
            return None

        active_state = self.hass.states.get(active_entity_id)
        pin_state = self.hass.states.get(pin_entity_id)

        if not active_state or not pin_state:
            return None

        if active_state.state == STATE_ON and pin_state.state.isnumeric():
            _LOGGER.debug(
                "PIN is masked for lock %s code slot %s, assuming value from PIN entity %s",
                self.lock.entity_id,
                code_slot,
                pin_entity_id,
            )
            return pin_state.state
        return None

    @callback
    def subscribe_push_updates(self) -> None:
        """Subscribe to User Code CC value update events."""
        # Idempotent - skip if already subscribed
        if self._value_update_unsub is not None:
            return

        ready, reason = self._get_client_state()
        if not ready:
            _LOGGER.debug(
                "Lock %s: push subscription deferred (%s)",
                self.lock.entity_id,
                reason,
            )
            self._schedule_push_retry(reason)
            return

        @callback
        def on_value_updated(event: dict[str, Any]) -> None:
            """Handle value update events from Z-Wave JS."""
            args: dict[str, Any] = event["args"]
            # Filter for User Code CC userCode property only (not userIdStatus)
            if (
                args.get("commandClass") != CommandClass.USER_CODE
                or args.get("property") != "userCode"
            ):
                return

            code_slot = int(args["propertyKey"])

            # Slot 0 is not a valid user code slot (used for status/metadata)
            if code_slot == 0:
                return

            # newValue is the raw PIN string (or None/empty/"0000" if cleared)
            new_value = args.get("newValue")
            # Treat empty, None, or all-zeros as cleared
            if not new_value or (
                isinstance(new_value, str) and new_value.strip("0") == ""
            ):
                value = ""
            else:
                value = str(new_value)

            # Handle masked codes (all asterisks) - resolve to expected PIN
            # This prevents infinite sync loops when locks return masked values
            if self._is_masked(value):
                resolved = self._resolve_masked(code_slot)
                if resolved is None:
                    # Can't resolve masked code, skip update to prevent loop
                    _LOGGER.debug(
                        "Lock %s: skipping masked code update for slot %s "
                        "(unable to resolve)",
                        self.lock.entity_id,
                        code_slot,
                    )
                    return
                value = resolved

            # Skip if value hasn't changed (Z-Wave JS sends duplicate events)
            if self.coordinator and self.coordinator.data.get(code_slot) == value:
                return

            _LOGGER.debug(
                "Lock %s received push update for slot %s: %s",
                self.lock.entity_id,
                code_slot,
                "****" if value else "(cleared)",
            )

            # Push update to coordinator
            if self.coordinator:
                self.coordinator.push_update({code_slot: value})

        try:
            self._value_update_unsub = self.node.on("value updated", on_value_updated)
        except ValueError as err:
            _LOGGER.debug(
                "Lock %s push subscription deferred: %s", self.lock.entity_id, err
            )
            self._schedule_push_retry("node not ready")

    def _schedule_push_retry(self, reason: str) -> None:
        """Schedule a retry for push subscription if one isn't pending."""
        if self._push_retry_cancel:
            return

        _LOGGER.debug(
            "Lock %s: scheduling push subscription retry in %ss (%s)",
            self.lock.entity_id,
            PUSH_SUBSCRIBE_RETRY_DELAY,
            reason,
        )
        self._push_retry_cancel = async_call_later(
            self.hass, PUSH_SUBSCRIBE_RETRY_DELAY, self._handle_push_retry
        )

    @callback
    def _handle_push_retry(self, _now: Any) -> None:
        """Retry push subscription after delay."""
        if self._push_retry_cancel:
            self._push_retry_cancel()
            self._push_retry_cancel = None
        self.subscribe_push_updates()

    @callback
    def unsubscribe_push_updates(self) -> None:
        """Unsubscribe from value update events."""
        if self._push_retry_cancel:
            self._push_retry_cancel()
            self._push_retry_cancel = None
        if self._value_update_unsub:
            self._value_update_unsub()
            self._value_update_unsub = None

    @callback
    def _zwave_js_event_filter(self, event_data: dict[str, Any]) -> bool:
        """Filter out events."""
        # Try to find the lock that we are getting an event for, skipping
        # ones that don't match
        assert self.node.client.driver
        return (
            event_data[ATTR_HOME_ID] == self.node.client.driver.controller.home_id
            and event_data[ATTR_NODE_ID] == self.node.node_id
            and event_data[ATTR_DEVICE_ID] == self.lock.device_id
        )

    @callback
    def _handle_zwave_js_event(self, evt: Event) -> None:
        """Handle Z-Wave JS event."""
        if evt.data[ATTR_TYPE] != NotificationType.ACCESS_CONTROL:
            _LOGGER.debug(
                "Lock %s received non Access Control event: %s",
                self.lock.entity_id,
                evt.as_dict(),
            )
            return

        params = evt.data.get(ATTR_PARAMETERS) or {}
        code_slot = params.get("userId", 0)
        self.async_fire_code_slot_event(
            code_slot=code_slot,
            to_locked=next(
                (
                    to_locked
                    for to_locked, codes in ACCESS_CONTROL_NOTIFICATION_TO_LOCKED.items()
                    if evt.data[ATTR_EVENT] in codes
                ),
                None,
            ),
            action_text=evt.data.get(ATTR_EVENT_LABEL),
            source_data=evt,
        )

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return ZWAVE_JS_DOMAIN

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up lock."""
        await super().async_setup(config_entry)
        self._listeners.append(
            self.hass.bus.async_listen(
                ZWAVE_JS_NOTIFICATION_EVENT,
                self._handle_zwave_js_event,
                self._zwave_js_event_filter,
            )
        )

    async def async_unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        for listener in self._listeners:
            listener()
        self._listeners.clear()
        await super().async_unload(remove_permanently)

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        ready, _reason = self._get_client_state()
        return ready

    async def async_hard_refresh_codes(self) -> dict[int, int | str]:
        """
        Perform hard refresh and return all codes.

        Uses Z-Wave JS's refresh_cc_values which handles checksum optimization
        internally - it will skip re-fetching codes if the checksum hasn't changed.
        Returns codes in the same format as async_get_usercodes().
        """
        await self._async_refresh_usercode_cache()
        return await self.async_get_usercodes()

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> bool:
        """
        Set a usercode on a code slot.

        Returns True if the value was changed, False if already set to this value.
        """
        # Check if the code is already set to this value (avoid unnecessary network call)
        usercode = str(usercode)
        try:
            if (current := get_usercode(self.node, code_slot)).get("in_use"):
                current_code = str(current.get("usercode", ""))
                # Handle masked codes (all asterisks) - resolve to expected PIN
                if self._is_masked(current_code) and usercode == self._resolve_masked(
                    code_slot
                ):
                    _LOGGER.debug(
                        "Lock %s slot %s has masked PIN matching expected, "
                        "skipping set",
                        self.lock.entity_id,
                        code_slot,
                    )
                    return False
                # Direct match - code is already set
                if usercode == current_code:
                    _LOGGER.debug(
                        "Lock %s slot %s already has this PIN, skipping set",
                        self.lock.entity_id,
                        code_slot,
                    )
                    return False
        except Exception:
            # If we can't check the cache, proceed with the set
            pass

        service_data = {
            ATTR_ENTITY_ID: self.lock.entity_id,
            ATTR_CODE_SLOT: code_slot,
            ATTR_USERCODE: usercode,
        }
        await self.async_call_service(
            ZWAVE_JS_DOMAIN, SERVICE_SET_LOCK_USERCODE, service_data
        )
        return True

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.
        """
        # Check if the slot is already cleared (avoid unnecessary network call)
        try:
            current = get_usercode(self.node, code_slot)
            if not current.get("in_use"):
                _LOGGER.debug(
                    "Lock %s slot %s already cleared, skipping clear",
                    self.lock.entity_id,
                    code_slot,
                )
                return False
        except Exception:
            # If we can't check the cache, proceed with the clear
            pass

        service_data = {
            ATTR_ENTITY_ID: self.lock.entity_id,
            ATTR_CODE_SLOT: code_slot,
        }
        await self.async_call_service(
            ZWAVE_JS_DOMAIN, SERVICE_CLEAR_LOCK_USERCODE, service_data
        )
        return True

    def _get_usercodes_from_cache(self) -> list[dict[str, Any]]:
        """Get usercodes from Z-Wave JS value DB cache."""
        try:
            return list(get_usercodes(self.node) or [])
        except Exception as err:
            raise LockDisconnected from err

    async def _async_refresh_usercode_cache(self) -> None:
        """Refresh usercode cache from the device."""
        try:
            await self.node.async_refresh_cc_values(CommandClass.USER_CODE)
        except Exception as err:
            raise LockDisconnected from err

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Get dictionary of code slots and usercodes."""
        code_slots = {
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
        }
        data: dict[int, int | str] = {}

        if not await self.async_is_connection_up():
            raise LockDisconnected

        slots = self._get_usercodes_from_cache()
        slots_by_num = {int(slot["code_slot"]): slot for slot in slots}

        # If any configured slot is missing or has unknown state, do one hard
        # refresh to populate the cache. This is more efficient than fetching
        # individual slots and uses Z-Wave JS's checksum optimization.
        # Note: We call _async_refresh_usercode_cache directly here to avoid
        # recursion since async_hard_refresh_codes calls async_get_usercodes.
        if any(
            slot_num not in slots_by_num or slots_by_num[slot_num].get("in_use") is None
            for slot_num in code_slots
        ):
            _LOGGER.debug(
                "Lock %s has missing/unknown slots, performing hard refresh",
                self.lock.entity_id,
            )
            await self._async_refresh_usercode_cache()
            slots = self._get_usercodes_from_cache()
            slots_by_num = {int(slot["code_slot"]): slot for slot in slots}

        slots_with_pin: list[int] = []
        slots_not_enabled: list[int] = []
        for slot in slots:
            code_slot = int(slot["code_slot"])
            usercode: str = slot["usercode"] or ""
            in_use: bool | None = slot["in_use"]

            if not in_use:
                slots_not_enabled.append(code_slot)
                data[code_slot] = ""
            # Handle masked usercodes (all *'s) - resolve to expected PIN
            elif self._is_masked(usercode):
                resolved = self._resolve_masked(code_slot)
                if resolved:
                    slots_with_pin.append(code_slot)
                    data[code_slot] = resolved
                # Can't resolve - skip slot entirely, making it unavailable
                # until entities are ready and resolution succeeds
            else:
                slots_with_pin.append(code_slot)
                data[code_slot] = usercode or ""

        _LOGGER.debug(
            "Lock %s: %s slots with PIN %s, %s slots not enabled %s",
            self.lock.entity_id,
            len(slots_with_pin),
            slots_with_pin,
            len(slots_not_enabled),
            slots_not_enabled,
        )
        return data
