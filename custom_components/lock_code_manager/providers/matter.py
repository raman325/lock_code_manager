"""Matter lock provider.

Handles PIN credential management via Matter lock services.
PINs are write-only: occupied slots report SlotCode.UNKNOWN, cleared slots report
SlotCode.EMPTY. Subscribes to DoorLock cluster events via the push framework for
code slot tracking (LockOperation) and occupancy updates (LockUserChange).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from matter_server.common.models import EventType

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from ..data import get_managed_slots
from ..exceptions import (
    DuplicateCodeError,
    LockCodeManagerProviderError,
    LockDisconnected,
)
from ..models import SlotCode
from ._base import BaseLock
from .const import LOGGER

MATTER_DOMAIN = "matter"

# DoorLock cluster ID (0x0101 = 257)
_DOOR_LOCK_CLUSTER_ID = 257

# DoorLock cluster event IDs
_LOCK_OPERATION_EVENT_ID = 2
_LOCK_USER_CHANGE_EVENT_ID = 4

# LockUserChange LockDataType values
_LOCK_DATA_TYPE_PIN = 6

# LockUserChange DataOperationType values
_DATA_OP_ADD = 0
_DATA_OP_CLEAR = 1
_DATA_OP_MODIFY = 2


@dataclass(repr=False, eq=False)
class MatterLock(BaseLock):
    """Class to represent a Matter lock."""

    _event_unsub: Callable[[], None] | None = field(
        default=None, init=False, repr=False
    )

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return MATTER_DOMAIN

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return True

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates.

        Matter locks push occupancy changes via LockUserChange events.
        PINs are still write-only (values are never pushed), but slot
        occupancy (UNKNOWN/EMPTY) is pushed in real time.
        """
        return True

    @property
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes."""
        return timedelta(minutes=5)

    @property
    def _matter_node_id(self) -> int | None:
        """Resolve the Matter node ID from the device registry."""
        if not self.device_entry:
            return None
        for domain, identifier in self.device_entry.identifiers:
            if domain == MATTER_DOMAIN:
                # Matter device identifiers are "{node_id}"
                try:
                    return int(identifier)
                except (ValueError, TypeError):
                    continue
        return None

    def _get_matter_client(self) -> Any | None:
        """Get the MatterClient from hass.data."""
        matter_data = self.hass.data.get(MATTER_DOMAIN)
        if not matter_data:
            return None
        try:
            entry_data = next(iter(matter_data.values()))
            return entry_data.adapter.matter_client
        except (StopIteration, AttributeError):
            return None

    async def _async_call_service(
        self,
        service: str,
        service_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a Matter service and return the per-entity response data.

        Intentionally does NOT route through ``BaseLock.async_call_service``.
        That helper does exactly one thing: wrap raw service-call failures
        (``HomeAssistantError`` family) as ``LockDisconnected``. Matter
        needs two additional jobs the base helper deliberately doesn't do:

        1. Unwrap the ``{entity_id: {...}}``-shaped response the Matter
           service returns and hand back the inner per-entity dict.
        2. Validate that response shape and raise
           ``LockCodeManagerProviderError`` (a different error class) when
           the lock returns no/non-dict data — a "malformed response" path
           the base helper has no opinion on at all.

        Future refactors should preserve this bypass rather than "unifying"
        it back into the base helper.
        """
        entity_id = self.lock.entity_id
        try:
            result = await self.hass.services.async_call(
                MATTER_DOMAIN,
                service,
                service_data,
                blocking=True,
                return_response=True,
            )
        except (ServiceValidationError, HomeAssistantError) as err:
            raise LockDisconnected(
                f"Matter service {MATTER_DOMAIN}.{service} failed for "
                f"{entity_id}: {err}"
            ) from err
        if not isinstance(result, dict) or entity_id not in result:
            raise LockCodeManagerProviderError(
                f"Matter service {MATTER_DOMAIN}.{service} returned no data for "
                f"{entity_id}"
            )
        entity_data = result[entity_id]
        if not isinstance(entity_data, dict):
            raise LockCodeManagerProviderError(
                f"Matter service {MATTER_DOMAIN}.{service} returned non-dict data "
                f"for {entity_id}: {type(entity_data).__name__}"
            )
        return entity_data

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Validate the lock supports Matter user management."""
        lock_info = await self._async_call_service(
            "get_lock_info",
            {"entity_id": self.lock.entity_id},
        )
        if not lock_info.get("supports_user_management"):
            raise LockCodeManagerProviderError(
                f"Matter lock {self.lock.entity_id} does not support user management"
            )
        if "pin" not in (lock_info.get("supported_credential_types") or []):
            raise LockCodeManagerProviderError(
                f"Matter lock {self.lock.entity_id} does not support PIN credentials"
            )
        LOGGER.debug(
            "Matter lock %s setup complete: %s",
            self.lock.entity_id,
            lock_info,
        )

    async def async_is_device_available(self) -> bool:
        """Return whether the Matter lock device is available for commands."""
        try:
            await self._async_call_service(
                "get_lock_info",
                {"entity_id": self.lock.entity_id},
            )
        except LockCodeManagerProviderError as err:
            LOGGER.debug(
                "Lock %s: availability check failed: %s",
                self.lock.entity_id,
                err,
            )
            return False
        return True

    # -- Event subscription via push framework --------------------------------

    @callback
    def setup_push_subscription(self) -> None:
        """Subscribe to Matter DoorLock cluster events.

        Handles two event types:
        - LockOperation (event 2): fires code slot events when a PIN is used
        - LockUserChange (event 4): pushes occupancy updates to coordinator
          when credentials are added, modified, or cleared

        Called by BaseLock.subscribe_push_updates() with automatic retry.
        """
        if self._event_unsub is not None:
            return

        client = self._get_matter_client()
        node_id = self._matter_node_id
        if not client or node_id is None:
            raise LockDisconnected(
                f"Matter client or node ID unavailable for {self.lock.entity_id}"
            )

        self._event_unsub = client.subscribe_events(
            callback=self._on_node_event,
            event_filter=EventType.NODE_EVENT,
            node_filter=node_id,
        )
        LOGGER.debug(
            "Lock %s: subscribed to Matter events (node %s)",
            self.lock.entity_id,
            node_id,
        )

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from Matter DoorLock cluster events."""
        if self._event_unsub:
            self._event_unsub()
            self._event_unsub = None

    @callback
    def _on_node_event(self, _event: Any, node_event: Any) -> None:
        """Dispatch DoorLock cluster events to the appropriate handler."""
        if getattr(node_event, "cluster_id", None) != _DOOR_LOCK_CLUSTER_ID:
            return

        event_id = getattr(node_event, "event_id", None)
        if event_id == _LOCK_OPERATION_EVENT_ID:
            self._handle_lock_operation(node_event)
        elif event_id == _LOCK_USER_CHANGE_EVENT_ID:
            self._handle_lock_user_change(node_event)
        else:
            LOGGER.debug(
                "Lock %s: unhandled DoorLock event_id=%s",
                self.lock.entity_id,
                event_id,
            )

    @callback
    def _handle_lock_operation(self, node_event: Any) -> None:
        """Handle LockOperation events (event ID 2).

        Fires a code slot event when a PIN credential is used to lock/unlock.
        Only PIN credentials (credentialType=1) trigger the event — other
        credential types (RFID, fingerprint, etc.) are ignored.
        """
        data: dict[str, Any] = getattr(node_event, "data", None) or {}
        credentials = data.get("credentials")
        lock_operation_type = data.get("lockOperationType")

        # Must have credentials with a PIN type to fire
        if not credentials:
            return

        # Find the PIN credential index (credentialType 1 = PIN)
        code_slot: int | None = None
        for cred in credentials:
            if isinstance(cred, dict) and cred.get("credentialType") == 1:
                code_slot = cred.get("credentialIndex")
                break

        # Only fire for PIN credentials
        if code_slot is None:
            return

        # Determine lock/unlock from operation type
        # 0 = Lock, 1 = Unlock, 2 = NonAccessUserEvent, 3 = ForcedUserEvent, 4 = Unlatch
        if lock_operation_type == 0:
            to_locked = True
        elif lock_operation_type == 1:
            to_locked = False
        else:
            to_locked = None

        LOGGER.debug(
            "Lock %s: LockOperation event — slot=%s, locked=%s",
            self.lock.entity_id,
            code_slot,
            to_locked,
        )

        self.async_fire_code_slot_event(
            code_slot=code_slot,
            to_locked=to_locked,
            action_text="locked"
            if to_locked
            else "unlocked"
            if to_locked is False
            else "operated",
            source_data=data,
        )

    @callback
    def _handle_lock_user_change(self, node_event: Any) -> None:
        """Handle LockUserChange events (event ID 4).

        Pushes occupancy updates to the coordinator when a PIN credential is
        added, modified, or cleared. This provides real-time change detection
        without waiting for the next poll cycle.

        Only PIN credentials (LockDataType=6) are handled. The DataIndex field
        maps to the credential index (code slot number).
        """
        data: dict[str, Any] = getattr(node_event, "data", None) or {}

        # Only handle PIN credential changes (LockDataType 6 = PIN)
        if data.get("lockDataType") != _LOCK_DATA_TYPE_PIN:
            return

        raw_index = data.get("dataIndex")
        if raw_index is None:
            return
        try:
            code_slot = int(raw_index)
        except (TypeError, ValueError):
            LOGGER.warning(
                "Lock %s: LockUserChange has non-integer dataIndex %r, ignoring",
                self.lock.entity_id,
                raw_index,
            )
            return

        operation = data.get("dataOperationType")

        if operation == _DATA_OP_CLEAR:
            resolved: str | SlotCode = SlotCode.EMPTY
        elif operation in (_DATA_OP_ADD, _DATA_OP_MODIFY):
            resolved = SlotCode.UNKNOWN
        else:
            LOGGER.debug(
                "Lock %s: LockUserChange event with unknown operation %s for slot %s",
                self.lock.entity_id,
                operation,
                code_slot,
            )
            return

        LOGGER.debug(
            "Lock %s: LockUserChange event — slot=%s, operation=%s, resolved=%s",
            self.lock.entity_id,
            code_slot,
            operation,
            resolved,
        )

        if self.coordinator and self.coordinator.data is not None:
            self.coordinator.push_update({code_slot: resolved})

    # -- Usercode CRUD -------------------------------------------------------

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Get dictionary of code slots and usercodes.

        Returns all occupied PIN credential slots as SlotCode.UNKNOWN (PINs are
        write-only) and managed empty slots as SlotCode.EMPTY. Unmanaged
        occupied slots are included so callers like the lock reset config flow
        step can detect codes not managed by Lock Code Manager.
        """
        managed_slots = get_managed_slots(self.hass, self.lock.entity_id)

        lock_data = await self._async_call_service(
            "get_lock_users",
            {"entity_id": self.lock.entity_id},
        )
        users = lock_data.get("users")
        if not isinstance(users, list):
            raise LockCodeManagerProviderError(
                f"Matter get_lock_users response for {self.lock.entity_id} "
                f"has unexpected 'users' value: {users!r}"
            )

        # Build a set of credential indices that have PIN credentials
        occupied_slots: set[int] = set()
        for user in users:
            for credential in user.get("credentials", []):
                if credential.get("credential_type") != "pin":
                    continue
                cred_index = credential.get("credential_index")
                if cred_index is None:
                    continue
                try:
                    occupied_slots.add(int(cred_index))
                except (TypeError, ValueError):
                    LOGGER.warning(
                        "Lock %s: skipping credential with invalid index %r",
                        self.lock.entity_id,
                        cred_index,
                    )
                    continue

        all_slots = managed_slots | occupied_slots
        LOGGER.debug(
            "Lock %s: %s managed, %s occupied, %s total",
            self.lock.entity_id,
            len(managed_slots),
            len(occupied_slots),
            len(all_slots),
        )
        return {
            slot: SlotCode.UNKNOWN if slot in occupied_slots else SlotCode.EMPTY
            for slot in all_slots
        }

    async def async_set_usercode(
        self, code_slot: int, usercode: str, name: str | None = None
    ) -> bool:
        """Set a usercode on a code slot.

        Returns True unconditionally because Matter does not reveal whether
        the credential value actually changed. Pushes SlotCode.UNKNOWN to the
        coordinator immediately — the LockUserChange event will confirm.

        If the lock returns a "duplicate" status, the PIN already exists on
        another slot. This raises DuplicateCodeError so the sync manager can
        disable the slot and notify the user.
        """
        try:
            await self._async_call_service(
                "set_lock_credential",
                {
                    "entity_id": self.lock.entity_id,
                    "credential_type": "pin",
                    "credential_data": usercode,
                    "credential_index": code_slot,
                },
            )
        except LockDisconnected as err:
            if "duplicate" in str(err).lower():
                raise DuplicateCodeError(
                    code_slot=code_slot,
                    lock_entity_id=self.lock.entity_id,
                ) from err
            raise
        if name is not None:
            try:
                await self._async_call_service(
                    "set_lock_user",
                    {
                        "entity_id": self.lock.entity_id,
                        "credential_index": code_slot,
                        "user_name": name,
                    },
                )
            except LockCodeManagerProviderError:
                LOGGER.warning(
                    "Lock %s: credential set on slot %s but failed to set "
                    "user name '%s'",
                    self.lock.entity_id,
                    code_slot,
                    name,
                )
        # Optimistic update: service call succeeded, push occupancy state
        # immediately. The LockUserChange event will confirm later.
        if self.coordinator and self.coordinator.data is not None:
            self.coordinator.push_update({code_slot: SlotCode.UNKNOWN})
        return True

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear a usercode on a code slot.

        Returns True if a credential was cleared, False if the slot was already
        empty. Pushes SlotCode.EMPTY to the coordinator immediately on success.
        """
        lock_data = await self._async_call_service(
            "get_lock_credential_status",
            {
                "entity_id": self.lock.entity_id,
                "credential_type": "pin",
                "credential_index": code_slot,
            },
        )
        if not lock_data.get("credential_exists"):
            return False

        await self._async_call_service(
            "clear_lock_credential",
            {
                "entity_id": self.lock.entity_id,
                "credential_type": "pin",
                "credential_index": code_slot,
            },
        )
        # Optimistic update: clear succeeded, push empty state immediately.
        # The LockUserChange event will confirm later.
        if self.coordinator and self.coordinator.data is not None:
            self.coordinator.push_update({code_slot: SlotCode.EMPTY})
        return True

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Perform hard refresh and return all codes.

        Matter has no cache to invalidate, so this is identical to async_get_usercodes.
        """
        return await self.async_get_usercodes()
