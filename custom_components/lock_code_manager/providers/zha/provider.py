"""ZHA lock provider implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from zigpy.zcl.clusters.closures import DoorLock

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback

from ...const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from ...data import get_entry_data
from ...exceptions import LockDisconnected
from .._base import BaseLock
from .const import OPERATION_SOURCE_NAMES, OPERATION_TO_LOCKED
from .helpers import get_zha_gateway

_LOGGER = logging.getLogger(__name__)


@dataclass(repr=False, eq=False)
class ZHALock(BaseLock):
    """Class to represent ZHA lock.

    Supports push updates via zigpy cluster listeners for:
    - programming_event_notification (0x21): PIN code added/deleted/changed
    - operation_event_notification (0x20): lock/unlock operations with user ID

    If the lock doesn't support programming event notifications (detected via
    event mask attributes), falls back to drift detection polling.
    """

    lock_config_entry: ConfigEntry = field(repr=False)
    _door_lock_cluster: DoorLock | None = field(init=False, default=None)
    _endpoint_id: int | None = field(init=False, default=None)
    _cluster_listener_unsub: Callable[[], None] | None = field(init=False, default=None)
    _supports_programming_events: bool | None = field(init=False, default=None)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return ZHA_DOMAIN

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates.

        Always True - we subscribe to cluster events for operation notifications
        (lock/unlock with user ID). Programming event support is checked separately
        to determine if we need drift detection fallback.
        """
        return True

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return interval for hard refresh.

        Returns 1 hour if the lock doesn't support programming event notifications
        (detected during setup), otherwise None to disable drift detection.
        """
        if self._supports_programming_events is False:
            return timedelta(hours=1)
        return None

    @property
    def connection_check_interval(self) -> timedelta | None:
        """Return interval for connection checks."""
        return timedelta(seconds=30)

    def _get_door_lock_cluster(self) -> DoorLock | None:
        """Get the Door Lock cluster for this device."""
        if self._door_lock_cluster is not None:
            return self._door_lock_cluster

        gateway = get_zha_gateway(self.hass)
        if not gateway:
            _LOGGER.debug("ZHA gateway not available")
            return None

        # Get device from entity
        entity_ref = gateway.get_entity_reference(self.lock.entity_id)
        if not entity_ref:
            _LOGGER.debug("Could not find entity reference for %s", self.lock.entity_id)
            return None

        device_proxy = entity_ref.entity_data.device_proxy
        if not device_proxy:
            _LOGGER.debug("Could not find device proxy for %s", self.lock.entity_id)
            return None

        # Get the underlying zigpy device (device_proxy.device is ZHA Device,
        # device_proxy.device.device is the zigpy device)
        zha_device = device_proxy.device
        if not zha_device:
            _LOGGER.debug("Could not find ZHA device for %s", self.lock.entity_id)
            return None

        zigpy_device = zha_device.device
        if not zigpy_device:
            _LOGGER.debug("Could not find zigpy device for %s", self.lock.entity_id)
            return None

        # Find the Door Lock cluster
        for endpoint_id, endpoint in zigpy_device.endpoints.items():
            if endpoint_id == 0:  # Skip ZDO endpoint
                continue
            for cluster in endpoint.in_clusters.values():
                if cluster.cluster_id == DoorLock.cluster_id:
                    self._door_lock_cluster = cluster
                    self._endpoint_id = endpoint_id
                    _LOGGER.debug(
                        "Found Door Lock cluster on endpoint %s for %s",
                        endpoint_id,
                        self.lock.entity_id,
                    )
                    return cluster

        _LOGGER.warning("Could not find Door Lock cluster for %s", self.lock.entity_id)
        return None

    async def _get_connected_cluster(self) -> DoorLock:
        """Get the Door Lock cluster, ensuring connection is up.

        Raises LockDisconnected if cluster is unavailable or device is disconnected.
        """
        cluster = self._get_door_lock_cluster()
        if not cluster:
            raise LockDisconnected("Door Lock cluster not available")

        if not await self.async_is_connection_up():
            raise LockDisconnected("Lock not connected")

        return cluster

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        gateway = get_zha_gateway(self.hass)
        if not gateway:
            return False

        entity_ref = gateway.get_entity_reference(self.lock.entity_id)
        if not entity_ref:
            return False

        device_proxy = entity_ref.entity_data.device_proxy
        if not device_proxy:
            return False

        # Check if device is available
        return device_proxy.device.available

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Get dictionary of code slots and usercodes."""
        cluster = await self._get_connected_cluster()

        # Get configured code slots for this lock
        code_slots = {
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
        }

        data: dict[int, int | str] = {}

        for slot_num in code_slots:
            try:
                # Call get_pin_code cluster command (0x06)
                result = await cluster.get_pin_code(slot_num)
                _LOGGER.debug(
                    "Lock %s slot %s get_pin_code result: %s",
                    self.lock.entity_id,
                    slot_num,
                    result,
                )

                # Parse result - format depends on zigpy version
                # Typically returns a foundation.Status and response fields
                if hasattr(result, "user_status"):
                    user_status = result.user_status
                    pin_code = getattr(result, "code", "") or ""
                elif isinstance(result, (list, tuple)) and len(result) >= 4:
                    # Result format: [user_id, user_status, user_type, code]
                    user_status = result[1]
                    pin_code = result[3]
                else:
                    _LOGGER.warning(
                        "Unexpected get_pin_code response format for %s slot %s: %s",
                        self.lock.entity_id,
                        slot_num,
                        result,
                    )
                    data[slot_num] = ""
                    continue

                # Check if slot is in use
                if user_status == DoorLock.UserStatus.Enabled:
                    # Convert bytes to string if needed
                    if isinstance(pin_code, bytes):
                        pin_code = pin_code.decode("utf-8", errors="ignore")
                    data[slot_num] = str(pin_code) if pin_code else ""
                else:
                    data[slot_num] = ""

            except Exception as err:
                _LOGGER.debug(
                    "Failed to get PIN for %s slot %s: %s",
                    self.lock.entity_id,
                    slot_num,
                    err,
                )
                # Fall back to assuming empty if we can't read
                data[slot_num] = ""

        return data

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> bool:
        """Set a usercode on a code slot."""
        cluster = await self._get_connected_cluster()

        try:
            # Call set_pin_code cluster command (0x05)
            # Parameters: user_id, user_status, user_type, pin_code
            result = await cluster.set_pin_code(
                code_slot,
                DoorLock.UserStatus.Enabled,
                DoorLock.UserType.Unrestricted,
                str(usercode),
            )
            _LOGGER.debug(
                "Lock %s slot %s set_pin_code result: %s",
                self.lock.entity_id,
                code_slot,
                result,
            )

            # Check result status
            if hasattr(result, "status"):
                if result.status != 0:
                    _LOGGER.warning(
                        "set_pin_code failed for %s slot %s: status %s",
                        self.lock.entity_id,
                        code_slot,
                        result.status,
                    )
                    raise LockDisconnected(
                        f"set_pin_code failed: status {result.status}"
                    )

            return True

        except LockDisconnected:
            raise
        except Exception as err:
            _LOGGER.error(
                "Failed to set PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to set PIN: {err}") from err

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear a usercode on a code slot."""
        cluster = await self._get_connected_cluster()

        try:
            # Call clear_pin_code cluster command (0x07)
            result = await cluster.clear_pin_code(code_slot)
            _LOGGER.debug(
                "Lock %s slot %s clear_pin_code result: %s",
                self.lock.entity_id,
                code_slot,
                result,
            )

            # Check result status
            if hasattr(result, "status"):
                if result.status != 0:
                    _LOGGER.warning(
                        "clear_pin_code failed for %s slot %s: status %s",
                        self.lock.entity_id,
                        code_slot,
                        result.status,
                    )
                    raise LockDisconnected(
                        f"clear_pin_code failed: status {result.status}"
                    )

            return True

        except LockDisconnected:
            raise
        except Exception as err:
            _LOGGER.error(
                "Failed to clear PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to clear PIN: {err}") from err

    async def async_hard_refresh_codes(self) -> dict[int, int | str]:
        """Perform hard refresh and return all codes.

        For ZHA, we just re-read all codes from the lock.
        """
        return await self.async_get_usercodes()

    # Push update support via zigpy cluster listeners

    async def _async_check_programming_event_support(self) -> bool:
        """Check if the lock supports programming event notifications.

        Reads the programming event mask attributes to determine if the lock
        will send programming_event_notification when codes change.
        """
        cluster = self._get_door_lock_cluster()
        if not cluster:
            return False

        # Check if any programming event mask attribute has a non-zero value
        # Use AttributeDefs for guaranteed-accurate IDs
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
            except Exception as err:
                _LOGGER.debug(
                    "Lock %s: could not read %s [0x%04x]: %s",
                    self.lock.entity_id,
                    attr.name,
                    attr.id,
                    err,
                )

        _LOGGER.debug(
            "Lock %s: no programming event mask attributes found, "
            "will use drift detection fallback",
            self.lock.entity_id,
        )
        return False

    @callback
    def subscribe_push_updates(self) -> None:
        """Subscribe to push-based value updates via zigpy cluster listener."""
        if self._cluster_listener_unsub is not None:
            return  # Already subscribed

        cluster = self._get_door_lock_cluster()
        if not cluster:
            _LOGGER.debug(
                "Lock %s: cannot subscribe to push updates - cluster not available",
                self.lock.entity_id,
            )
            return

        # Check programming event support if not already done
        if self._supports_programming_events is None:
            # Schedule async check - for now assume supported, will update on next refresh
            self.hass.async_create_task(
                self._async_detect_programming_support(),
                f"Detect programming event support for {self.lock.entity_id}",
            )

        # Register as a listener on the cluster
        cluster.add_listener(self)
        self._cluster_listener_unsub = lambda: cluster.remove_listener(self)

        _LOGGER.debug(
            "Lock %s: subscribed to Door Lock cluster events",
            self.lock.entity_id,
        )

    async def _async_detect_programming_support(self) -> None:
        """Detect programming event support and log result."""
        self._supports_programming_events = (
            await self._async_check_programming_event_support()
        )
        if not self._supports_programming_events:
            _LOGGER.info(
                "Lock %s: programming event notifications not supported, "
                "enabling drift detection (1 hour interval)",
                self.lock.entity_id,
            )

    @callback
    def unsubscribe_push_updates(self) -> None:
        """Unsubscribe from push-based value updates."""
        if self._cluster_listener_unsub is not None:
            self._cluster_listener_unsub()
            self._cluster_listener_unsub = None
            _LOGGER.debug(
                "Lock %s: unsubscribed from Door Lock cluster events",
                self.lock.entity_id,
            )

    def cluster_command(
        self,
        tsn: int,
        command_id: int,
        args: Any,
    ) -> None:
        """Handle incoming cluster commands from the lock.

        Called by zigpy when the lock sends a cluster command (client -> server).
        """
        if command_id == DoorLock.ClientCommandDefs.programming_event_notification.id:
            self._handle_programming_event(args)
        elif command_id == DoorLock.ClientCommandDefs.operation_event_notification.id:
            self._handle_operation_event(args)

    def _handle_programming_event(self, args: Any) -> None:
        """Handle programming event notification (PIN added/deleted/changed).

        This triggers a coordinator refresh to pick up the new code state.
        """
        # Extract event details
        try:
            event_code = (
                args.program_event_code
                if hasattr(args, "program_event_code")
                else args[1]
            )
            user_id = args.user_id if hasattr(args, "user_id") else args[2]
        except (AttributeError, IndexError, TypeError):
            _LOGGER.debug(
                "Lock %s: could not parse programming event args: %s",
                self.lock.entity_id,
                args,
            )
            return

        _LOGGER.debug(
            "Lock %s: programming event - code=%s, user_id=%s",
            self.lock.entity_id,
            event_code,
            user_id,
        )

        # Trigger coordinator refresh to pick up the change
        if self.coordinator:
            self.hass.async_create_task(
                self.coordinator.async_request_refresh(),
                f"Refresh {self.lock.entity_id} after programming event",
            )

    def _handle_operation_event(self, args: Any) -> None:
        """Handle operation event notification (lock/unlock with user ID).

        This fires a code slot event so automations can react to lock usage.
        """
        # Extract event details
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
        except (AttributeError, IndexError, TypeError):
            _LOGGER.debug(
                "Lock %s: could not parse operation event args: %s",
                self.lock.entity_id,
                args,
            )
            return

        _LOGGER.debug(
            "Lock %s: operation event - source=%s, code=%s, user_id=%s",
            self.lock.entity_id,
            source,
            event_code,
            user_id,
        )

        # Determine if this is a lock or unlock event
        to_locked = OPERATION_TO_LOCKED.get(event_code)

        # Build action text from source and event
        source_name = OPERATION_SOURCE_NAMES.get(source, f"Source {source}")
        action = "lock" if to_locked else "unlock" if to_locked is False else "event"
        action_text = f"{source_name} {action} operation"

        # Fire code slot event (user_id 0 typically means no code was used)
        self.async_fire_code_slot_event(
            code_slot=user_id if user_id > 0 else None,
            to_locked=to_locked,
            action_text=action_text,
            source_data={
                "source": source,
                "event_code": event_code,
                "user_id": user_id,
            },
        )
