"""ZHA (Zigbee Home Automation) lock provider.

Communicates with Zigbee locks via the zigpy DoorLock cluster through ZHA.
Supports push updates via cluster listeners for operation events (lock/unlock
with user ID) and programming events (PIN code changes).  When the lock does
not support programming event notifications, falls back to drift detection
polling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any, Literal

from zigpy.zcl.clusters.closures import DoorLock

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.components.zha.helpers import (
    get_zha_gateway_proxy as _get_zha_gateway_proxy,
)
from homeassistant.core import callback

from ..exceptions import LockDisconnected
from ..models import SlotCode
from ._base import BaseLock

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ZCL DoorLock event mappings
# ---------------------------------------------------------------------------

OPERATION_TO_LOCKED: dict[int, bool] = {
    DoorLock.OperationEvent.Lock: True,
    DoorLock.OperationEvent.KeyLock: True,
    DoorLock.OperationEvent.AutoLock: True,
    DoorLock.OperationEvent.Manual_Lock: True,
    DoorLock.OperationEvent.ScheduleLock: True,
    DoorLock.OperationEvent.OnTouchLock: True,
    DoorLock.OperationEvent.Unlock: False,
    DoorLock.OperationEvent.KeyUnlock: False,
    DoorLock.OperationEvent.Manual_Unlock: False,
    DoorLock.OperationEvent.ScheduleUnlock: False,
}

OPERATION_SOURCE_NAMES: dict[int, str] = {
    DoorLock.OperationEventSource.Keypad: "Keypad",
    DoorLock.OperationEventSource.RF: "RF",
    DoorLock.OperationEventSource.Manual: "Manual",
    DoorLock.OperationEventSource.RFID: "RFID",
    DoorLock.OperationEventSource.Indeterminate: "Unknown",
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


@dataclass(repr=False, eq=False)
class ZHALock(BaseLock):
    """ZHA lock provider.

    Push updates come from zigpy cluster listeners:
    - ``programming_event_notification`` (0x21): PIN added/deleted/changed
    - ``operation_event_notification`` (0x20): lock/unlock with user ID

    If the lock does not support programming event notifications (detected via
    event mask attributes), falls back to hourly drift detection polling.
    """

    _door_lock_cluster: DoorLock | None = field(init=False, default=None)
    _endpoint_id: int | None = field(init=False, default=None)
    _cluster_listener_unsub: Callable[[], None] | None = field(init=False, default=None)
    _supports_programming_events: bool | None = field(init=False, default=None)

    # -- Properties ----------------------------------------------------------

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return ZHA_DOMAIN

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates.

        Always True — we subscribe to cluster events for operation
        notifications (lock/unlock with user ID).  Programming event support
        is checked separately to decide whether drift detection is needed.
        """
        return True

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return interval for drift detection.

        One hour if the lock lacks programming event notifications, otherwise
        None (push handles code changes).
        """
        if self._supports_programming_events is False:
            return timedelta(hours=1)
        return None

    @property
    def connection_check_interval(self) -> timedelta | None:
        """Return interval for connection checks."""
        return timedelta(seconds=30)

    # -- Cluster access ------------------------------------------------------

    def _get_door_lock_cluster(self) -> DoorLock | None:
        """Return the DoorLock cluster for this device, caching the result."""
        if self._door_lock_cluster is not None:
            return self._door_lock_cluster

        gateway = self._get_gateway()
        if not gateway:
            return None

        entity_ref = gateway.get_entity_reference(self.lock.entity_id)
        if not entity_ref:
            _LOGGER.debug("Could not find entity reference for %s", self.lock.entity_id)
            return None

        device_proxy = entity_ref.entity_data.device_proxy
        if not device_proxy:
            _LOGGER.debug("Could not find device proxy for %s", self.lock.entity_id)
            return None

        zigpy_device = device_proxy.device.device
        if not zigpy_device:
            _LOGGER.debug("Could not find zigpy device for %s", self.lock.entity_id)
            return None

        for endpoint_id, endpoint in zigpy_device.endpoints.items():
            if endpoint_id == 0:
                continue
            for cluster in endpoint.in_clusters.values():
                if cluster.cluster_id == DoorLock.cluster_id:
                    self._door_lock_cluster = cluster
                    self._endpoint_id = endpoint_id
                    _LOGGER.debug(
                        "Found DoorLock cluster on endpoint %s for %s",
                        endpoint_id,
                        self.lock.entity_id,
                    )
                    return cluster

        _LOGGER.warning("Could not find DoorLock cluster for %s", self.lock.entity_id)
        return None

    async def _get_connected_cluster(self) -> DoorLock:
        """Return a connected DoorLock cluster or raise LockDisconnected."""
        cluster = self._get_door_lock_cluster()
        if not cluster:
            raise LockDisconnected("DoorLock cluster not available")
        if not await self.async_is_integration_connected():
            raise LockDisconnected("Lock not connected")
        return cluster

    # -- Helpers -------------------------------------------------------------

    def _get_gateway(self) -> Any | None:
        """Return the ZHA gateway proxy, or None if unavailable."""
        try:
            return _get_zha_gateway_proxy(self.hass)
        except KeyError, ValueError:
            return None

    # -- Connection ----------------------------------------------------------

    async def async_is_integration_connected(self) -> bool:
        """Return whether ZHA is loaded and the device is available."""
        gateway = self._get_gateway()
        if not gateway:
            return False
        entity_ref = gateway.get_entity_reference(self.lock.entity_id)
        if not entity_ref:
            return False
        device_proxy = entity_ref.entity_data.device_proxy
        if not device_proxy:
            return False
        return device_proxy.device.available

    # -- Usercode operations -------------------------------------------------

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Read PIN codes from all managed slots."""
        cluster = await self._get_connected_cluster()
        managed = self.managed_slots
        if not managed:
            return {}

        data: dict[int, str | SlotCode] = {}
        for slot_num in managed:
            try:
                result = await cluster.get_pin_code(slot_num)
                _LOGGER.debug(
                    "Lock %s slot %s get_pin_code: %s",
                    self.lock.entity_id,
                    slot_num,
                    result,
                )
                user_status, pin_code = self._parse_pin_response(result)
                if user_status == DoorLock.UserStatus.Enabled and pin_code:
                    data[slot_num] = pin_code
                else:
                    data[slot_num] = SlotCode.EMPTY
            except LockDisconnected:
                raise
            except Exception:
                _LOGGER.debug(
                    "Lock %s: failed to read slot %s, assuming empty",
                    self.lock.entity_id,
                    slot_num,
                    exc_info=True,
                )
                data[slot_num] = SlotCode.EMPTY
        return data

    async def async_set_usercode(
        self,
        code_slot: int,
        usercode: str,
        name: str | None = None,
        source: Literal["sync", "direct"] = "direct",
    ) -> bool:
        """Set a PIN code on a slot."""
        cluster = await self._get_connected_cluster()
        try:
            result = await cluster.set_pin_code(
                code_slot,
                DoorLock.UserStatus.Enabled,
                DoorLock.UserType.Unrestricted,
                str(usercode),
            )
            _LOGGER.debug(
                "Lock %s slot %s set_pin_code: %s",
                self.lock.entity_id,
                code_slot,
                result,
            )
            if hasattr(result, "status") and result.status != 0:
                raise LockDisconnected(f"set_pin_code failed: status {result.status}")
            return True
        except LockDisconnected:
            raise
        except Exception as err:
            raise LockDisconnected(f"Failed to set PIN: {err}") from err

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear a PIN code from a slot."""
        cluster = await self._get_connected_cluster()
        try:
            result = await cluster.clear_pin_code(code_slot)
            _LOGGER.debug(
                "Lock %s slot %s clear_pin_code: %s",
                self.lock.entity_id,
                code_slot,
                result,
            )
            if hasattr(result, "status") and result.status != 0:
                raise LockDisconnected(f"clear_pin_code failed: status {result.status}")
            return True
        except LockDisconnected:
            raise
        except Exception as err:
            raise LockDisconnected(f"Failed to clear PIN: {err}") from err

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Re-read all codes from the lock (no cache to invalidate)."""
        return await self.async_get_usercodes()

    # -- Response parsing ----------------------------------------------------

    @staticmethod
    def _parse_pin_response(result: Any) -> tuple[int, str]:
        """Extract (user_status, pin_code) from a get_pin_code response."""
        if hasattr(result, "user_status"):
            pin = getattr(result, "code", "") or ""
            if isinstance(pin, bytes):
                pin = pin.decode("utf-8", errors="ignore")
            return result.user_status, str(pin)
        if isinstance(result, (list, tuple)) and len(result) >= 4:
            pin = result[3]
            if isinstance(pin, bytes):
                pin = pin.decode("utf-8", errors="ignore")
            return result[1], str(pin) if pin else ""
        return DoorLock.UserStatus.Available, ""

    # -- Push updates --------------------------------------------------------

    @callback
    def setup_push_subscription(self) -> None:
        """Subscribe to DoorLock cluster events."""
        if self._cluster_listener_unsub is not None:
            return

        cluster = self._get_door_lock_cluster()
        if not cluster:
            raise LockDisconnected(
                "DoorLock cluster not available for push subscription"
            )

        if self._supports_programming_events is None:
            self.hass.async_create_task(
                self._async_detect_programming_support(),
                f"Detect programming event support for {self.lock.entity_id}",
            )

        cluster.add_listener(self)
        self._cluster_listener_unsub = lambda: cluster.remove_listener(self)
        _LOGGER.debug(
            "Lock %s: subscribed to DoorLock cluster events",
            self.lock.entity_id,
        )

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from DoorLock cluster events."""
        if self._cluster_listener_unsub is not None:
            self._cluster_listener_unsub()
            self._cluster_listener_unsub = None
            _LOGGER.debug(
                "Lock %s: unsubscribed from DoorLock cluster events",
                self.lock.entity_id,
            )

    # -- Cluster listener callbacks ------------------------------------------

    def cluster_command(
        self,
        tsn: int,
        command_id: int,
        args: Any,
    ) -> None:
        """Handle incoming cluster commands from the lock (zigpy listener)."""
        if command_id == DoorLock.ClientCommandDefs.programming_event_notification.id:
            self._handle_programming_event(args)
        elif command_id == DoorLock.ClientCommandDefs.operation_event_notification.id:
            self._handle_operation_event(args)

    def _handle_programming_event(self, args: Any) -> None:
        """Handle programming event (PIN added/deleted/changed).

        Triggers a coordinator refresh to pick up the new code state.
        """
        try:
            event_code = (
                args.program_event_code
                if hasattr(args, "program_event_code")
                else args[1]
            )
            user_id = args.user_id if hasattr(args, "user_id") else args[2]
        except AttributeError, IndexError, TypeError:
            _LOGGER.debug(
                "Lock %s: could not parse programming event: %s",
                self.lock.entity_id,
                args,
            )
            return

        _LOGGER.debug(
            "Lock %s: programming event code=%s user_id=%s",
            self.lock.entity_id,
            event_code,
            user_id,
        )
        if self.coordinator:
            self.hass.async_create_task(
                self.coordinator.async_request_refresh(),
                f"Refresh {self.lock.entity_id} after programming event",
            )

    def _handle_operation_event(self, args: Any) -> None:
        """Handle operation event (lock/unlock with user ID).

        Fires a code slot event so automations can react to lock usage.
        """
        try:
            source = (
                args.operation_event_source
                if hasattr(args, "operation_event_source")
                else args[0]
            )
            event_code = (
                args.operation_event_code
                if hasattr(args, "operation_event_code")
                else args[1]
            )
            user_id = args.user_id if hasattr(args, "user_id") else args[2]
        except AttributeError, IndexError, TypeError:
            _LOGGER.debug(
                "Lock %s: could not parse operation event: %s",
                self.lock.entity_id,
                args,
            )
            return

        _LOGGER.debug(
            "Lock %s: operation event source=%s code=%s user_id=%s",
            self.lock.entity_id,
            source,
            event_code,
            user_id,
        )

        to_locked = OPERATION_TO_LOCKED.get(event_code)
        source_name = OPERATION_SOURCE_NAMES.get(source, f"Source {source}")
        action = "lock" if to_locked else "unlock" if to_locked is False else "event"

        self.async_fire_code_slot_event(
            code_slot=user_id if user_id > 0 else None,
            to_locked=to_locked,
            action_text=f"{source_name} {action} operation",
            source_data={
                "source": source,
                "event_code": event_code,
                "user_id": user_id,
            },
        )

    # -- Programming event support detection ---------------------------------

    async def _async_detect_programming_support(self) -> None:
        """Detect programming event support and update the property."""
        self._supports_programming_events = (
            await self._async_check_programming_event_support()
        )
        if not self._supports_programming_events:
            _LOGGER.info(
                "Lock %s: programming event notifications not supported, "
                "enabling drift detection (1 hour interval)",
                self.lock.entity_id,
            )

    async def _async_check_programming_event_support(self) -> bool:
        """Check if the lock supports programming event notifications.

        Reads event mask attributes to determine if the lock will send
        programming_event_notification when codes change.
        """
        cluster = self._get_door_lock_cluster()
        if not cluster:
            return False

        mask_attrs = (
            DoorLock.AttributeDefs.keypad_programming_event_mask,
            DoorLock.AttributeDefs.rf_programming_event_mask,
            DoorLock.AttributeDefs.rfid_programming_event_mask,
        )
        for attr in mask_attrs:
            try:
                if hasattr(cluster, "get"):
                    value = cluster.get(attr.name)
                    if value is not None and value != 0:
                        _LOGGER.debug(
                            "Lock %s: supports programming events (%s [0x%04x]=0x%04x)",
                            self.lock.entity_id,
                            attr.name,
                            attr.id,
                            value,
                        )
                        return True
            except Exception:
                _LOGGER.debug(
                    "Lock %s: could not read %s [0x%04x]",
                    self.lock.entity_id,
                    attr.name,
                    attr.id,
                    exc_info=True,
                )

        _LOGGER.debug(
            "Lock %s: no programming event mask attributes found, "
            "will use drift detection fallback",
            self.lock.entity_id,
        )
        return False
