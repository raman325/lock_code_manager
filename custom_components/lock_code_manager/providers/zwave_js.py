"""Z-Wave JS lock provider.

Handles push updates, duplicate code detection, and rate-limited set/clear operations.
See ARCHITECTURE.md for the provider's role in the data flow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import functools
import logging
from typing import Any

from zwave_js_server.client import Client
from zwave_js_server.const import CommandClass, NodeStatus
from zwave_js_server.const.command_class.lock import (
    ATTR_CODE_SLOT,
    ATTR_IN_USE,
    ATTR_USERCODE,
    LOCK_USERCODE_PROPERTY,
    LOCK_USERCODE_STATUS_PROPERTY,
    CodeSlotStatus,
)
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import (
    get_usercode,
    get_usercode_from_node,
    get_usercodes,
)

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
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import Event, callback

from ..const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from ..data import find_entry_for_lock_slot, get_entry_data
from ..exceptions import LockDisconnected
from ..models import SlotCode
from ..util import async_disable_slot
from ._base import BaseLock

_LOGGER = logging.getLogger(__name__)

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
    _set_in_progress_code_slot: int | None = field(init=False, default=None)

    @property
    def node(self) -> Node:
        """Return ZWave JS node."""
        return async_get_node_from_entity_id(
            self.hass, self.lock.entity_id, self.ent_reg
        )

    @functools.cached_property
    def _usercode_cc_version(self) -> int:
        """Return the User Code CC version supported by this node."""
        version = next(
            (
                cc.version
                for cc in self.node.command_classes
                if cc.id == CommandClass.USER_CODE
            ),
            0,
        )
        if version == 0:
            _LOGGER.warning(
                "Lock %s: User Code CC not found on node %s. This may "
                "indicate an incomplete interview. Defaulting to V1 behavior",
                self.lock.entity_id,
                self.node.node_id,
            )
            return 1
        return version

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

    def code_slot_in_use(self, code_slot: int) -> bool | None:
        """Return whether a code slot is in use."""
        try:
            return get_usercode(self.node, code_slot)[ATTR_IN_USE]
        except (KeyError, ValueError):
            return None

    def _slot_expects_pin(self, code_slot: int) -> bool:
        """
        Check if LCM expects a PIN on this slot (enabled with PIN configured).

        Uses coordinator.expected_codes instead of entity state so this works
        during startup before entities are populated. Used to ignore stale
        userIdStatus=AVAILABLE events from locks that report old status after a
        code was successfully set.
        """
        if not self.coordinator:
            return False
        return bool(self.coordinator.expected_codes.get(code_slot))

    @callback
    def _handle_usercode_status_update(self, code_slot: int, status: Any) -> None:
        """Handle userIdStatus value update for a code slot."""
        if status == CodeSlotStatus.AVAILABLE:
            # Ignore AVAILABLE status if Lock Code Manager expects a PIN on this
            # slot. Some locks send stale AVAILABLE events after a code was set,
            # which would cause infinite sync loops.
            if self._slot_expects_pin(code_slot):
                _LOGGER.debug(
                    "Lock %s: ignoring userIdStatus=AVAILABLE for slot %s "
                    "(LCM expects PIN on this slot)",
                    self.lock.entity_id,
                    code_slot,
                )
                return

            # Slot was cleared - update coordinator if needed
            if (
                self.coordinator
                and self.coordinator.data.get(code_slot) is not SlotCode.EMPTY
            ):
                _LOGGER.debug(
                    "Lock %s: slot %s userIdStatus=AVAILABLE, marking cleared",
                    self.lock.entity_id,
                    code_slot,
                )
                self.coordinator.push_update({code_slot: SlotCode.EMPTY})

    @callback
    def _handle_usercode_value_update(self, code_slot: int, new_value: Any) -> None:
        """Handle userCode value update for a code slot."""
        # Determine the resolved value as a SlotCode or plain string
        if not new_value or (isinstance(new_value, str) and new_value.strip("0") == ""):
            resolved: str | SlotCode = SlotCode.EMPTY
        else:
            value = str(new_value)
            code_slot_in_use = self.code_slot_in_use(code_slot)
            if value == "*" * len(value) and code_slot_in_use:
                resolved = SlotCode.UNKNOWN
            else:
                resolved = value

        # Skip if value hasn't changed (Z-Wave JS sends duplicate events)
        if self.coordinator and self.coordinator.data.get(code_slot) == resolved:
            return

        _LOGGER.debug(
            "Lock %s received push update for slot %s: %s",
            self.lock.entity_id,
            code_slot,
            "****"
            if resolved not in (SlotCode.EMPTY, SlotCode.UNKNOWN)
            else f"({resolved})",
        )

        # Push update to coordinator
        if self.coordinator:
            self.coordinator.push_update({code_slot: resolved})

    @callback
    def setup_push_subscription(self) -> None:
        """Subscribe to User Code CC value update events."""
        # Idempotent - skip if already subscribed
        if self._value_update_unsub is not None:
            return

        ready, reason = self._get_client_state()
        if not ready:
            raise LockDisconnected(reason)

        @callback
        def on_value_updated(event: dict[str, Any]) -> None:
            """Handle value update events from Z-Wave JS."""
            args: dict[str, Any] = event["args"]
            # Filter for User Code command class
            if args.get("commandClass") != CommandClass.USER_CODE:
                return

            property_name = args.get("property")
            if property_name not in (
                LOCK_USERCODE_PROPERTY,
                LOCK_USERCODE_STATUS_PROPERTY,
            ):
                return

            code_slot = int(args["propertyKey"])

            # Slot 0 is not a valid user code slot (used for status/metadata)
            if code_slot == 0:
                return

            # Clear in-progress tracking only on userCode updates for the slot
            # we were setting. userIdStatus updates don't confirm acceptance and
            # could race with duplicate-code notifications.
            if (
                property_name == LOCK_USERCODE_PROPERTY
                and code_slot == self._set_in_progress_code_slot
            ):
                self._set_in_progress_code_slot = None

            # Delegate to the appropriate handler
            if property_name == LOCK_USERCODE_STATUS_PROPERTY:
                self._handle_usercode_status_update(code_slot, args.get("newValue"))
            else:
                self._handle_usercode_value_update(code_slot, args.get("newValue"))

        try:
            self._value_update_unsub = self.node.on("value updated", on_value_updated)
        except ValueError as err:
            raise LockDisconnected(f"node not ready: {err}") from err

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from value update events."""
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

        # Handle duplicate code rejection — only when LCM initiated the set
        if (
            evt.data[ATTR_EVENT]
            == AccessControlNotificationEvent.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE
            and self._set_in_progress_code_slot is not None
            and code_slot in (0, self._set_in_progress_code_slot)
        ):
            slot = self._set_in_progress_code_slot or code_slot
            self.hass.async_create_task(
                self._async_handle_duplicate_code(),
                f"Handle duplicate code for {self.lock.entity_id} slot {slot}",
            )
            return

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

    async def _async_handle_duplicate_code(self) -> None:
        """Handle duplicate code rejection from the lock."""
        lock_ent_id = self.lock.entity_id
        code_slot = self._set_in_progress_code_slot
        self._set_in_progress_code_slot = None
        if code_slot is None:
            return
        entry = find_entry_for_lock_slot(self.hass, self.lock.entity_id, code_slot)
        if entry is None:
            return

        entry_title = entry.title or entry.entry_id
        reason = (
            f"Lock **{lock_ent_id}** rejected the code for slot **{code_slot}** "
            f"(config entry **{entry_title}**) because it duplicates a code "
            f"in another slot. Slot {code_slot} has been disabled. Please set a "
            f"unique code and re-enable slot {code_slot}"
        )
        _LOGGER.error(
            "Lock %s rejected the code for slot %s (config entry %s) because "
            "it duplicates a code in another slot. Slot %s has been disabled",
            lock_ent_id,
            code_slot,
            entry_title,
            code_slot,
        )
        await async_disable_slot(
            self.hass,
            self.ent_reg,
            entry.entry_id,
            code_slot,
            reason=reason,
            title="Duplicate Lock Code Rejected",
            lock_name=self.display_name,
            lock_entity_id=lock_ent_id,
        )

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return ZWAVE_JS_DOMAIN

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up lock by provider."""
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

    async def async_is_integration_connected(self) -> bool:
        """Return whether the Z-Wave JS client is connected."""
        ready, _reason = self._get_client_state()
        return ready

    async def async_is_device_available(self) -> bool:
        """Return whether the Z-Wave node is available for commands."""
        try:
            return self.node.status != NodeStatus.DEAD
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Lock %s: failed to check device availability: %s",
                self.lock.entity_id,
                err,
            )
            return False

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """
        Perform hard refresh and return all codes.

        Uses Z-Wave JS's refresh_cc_values which handles checksum optimization
        internally - it will skip re-fetching codes if the checksum hasn't changed.
        Returns codes in the same format as async_get_usercodes().
        """
        await self._async_refresh_usercode_cache()
        return await self.async_get_usercodes()

    async def async_set_usercode(
        self, code_slot: int, usercode: str, name: str | None = None
    ) -> bool:
        """
        Set a usercode on a code slot.

        Returns True if the value was changed, False if already set to this value.
        """
        # Check if the code is already set to this value (avoid unnecessary network call)
        try:
            if (current := get_usercode(self.node, code_slot)).get("in_use"):
                current_code = str(current.get("usercode", ""))
                # If masked (all asterisks), skip duplicate check since we cannot compare
                if current_code != "*" * len(current_code) and usercode == current_code:
                    _LOGGER.debug(
                        "Lock %s slot %s already has this PIN, skipping set",
                        self.lock.entity_id,
                        code_slot,
                    )
                    return False
        except Exception:
            # If we can't check the cache, proceed with the set
            pass

        self._set_in_progress_code_slot = code_slot
        service_data = {
            ATTR_ENTITY_ID: self.lock.entity_id,
            ATTR_CODE_SLOT: code_slot,
            ATTR_USERCODE: usercode,
        }
        await self.async_call_service(
            ZWAVE_JS_DOMAIN, SERVICE_SET_LOCK_USERCODE, service_data
        )
        # V1 locks don't reliably update the Z-Wave JS value cache after set.
        # Poll the slot directly from the device to force-update the cache
        # before the coordinator reads it, preventing sync loops.
        # No try-except: if the poll fails, we must not proceed with the
        # optimistic update since async_request_refresh() would overwrite it
        # with stale cache data, reintroducing the sync loop. Letting the
        # error propagate allows the sync mechanism to retry the operation.
        if self._usercode_cc_version < 2:
            await get_usercode_from_node(self.node, code_slot)
        # Optimistic update: Z-Wave command succeeded (lock acknowledged), but the
        # value cache updates asynchronously via push notification. Update coordinator
        # immediately to prevent sync loops from reading stale cache data.
        if self.coordinator:
            self.coordinator.push_update({code_slot: usercode})
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
        # V1 locks don't reliably update the Z-Wave JS value cache after clear.
        # Poll the slot directly from the device to force-update the cache
        # before the coordinator reads it, preventing sync loops.
        # See comment in async_set_usercode for why this is not wrapped in
        # try-except.
        if self._usercode_cc_version < 2:
            await get_usercode_from_node(self.node, code_slot)
        # Optimistic update: Z-Wave command succeeded (lock acknowledged), but the
        # value cache updates asynchronously via push notification. Update coordinator
        # immediately to prevent sync loops from reading stale cache data.
        if self.coordinator:
            self.coordinator.push_update({code_slot: SlotCode.EMPTY})
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

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Get dictionary of code slots and usercodes."""
        code_slots = {
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
        }
        data: dict[int, str | SlotCode] = {}

        if not await self.async_is_integration_connected():
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

        for slot in slots:
            code_slot = int(slot["code_slot"])
            usercode: str = slot["usercode"] or ""
            in_use: bool | None = slot["in_use"]

            if not in_use:
                data[code_slot] = SlotCode.EMPTY
            elif usercode == "*" * len(usercode) and in_use:
                # Masked code (all asterisks with slot in use)
                data[code_slot] = SlotCode.UNKNOWN
            elif usercode:
                # Unmasked code
                data[code_slot] = usercode

        slots_with_pin = [
            s for s, v in data.items() if v not in (SlotCode.EMPTY, SlotCode.UNKNOWN)
        ]
        slots_not_enabled = [s for s, v in data.items() if v is SlotCode.EMPTY]
        _LOGGER.debug(
            "Lock %s: %s slots with PIN %s, %s slots not enabled %s",
            self.lock.entity_id,
            len(slots_with_pin),
            slots_with_pin,
            len(slots_not_enabled),
            slots_not_enabled,
        )
        return data
