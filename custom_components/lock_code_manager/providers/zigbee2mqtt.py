"""Module for Zigbee2MQTT locks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import json
from typing import TYPE_CHECKING, Any

from homeassistant.components.mqtt import (
    DOMAIN as MQTT_DOMAIN,
    async_publish,
    async_subscribe,
)
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
from homeassistant.core import callback

from ..const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from ..data import get_entry_data
from ..exceptions import LockDisconnected
from ..models import SlotCode
from ._base import BaseLock
from .const import LOGGER

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage

# Default Zigbee2MQTT base topic
DEFAULT_BASE_TOPIC = "zigbee2mqtt"

# User status values per Zigbee Cluster Library specification (same as ZHA)
USER_STATUS_AVAILABLE = 0
USER_STATUS_ENABLED = 1


@dataclass(repr=False, eq=False)
class Zigbee2MQTTLock(BaseLock):
    """Class to represent Zigbee2MQTT lock."""

    _base_topic: str = field(init=False, default=DEFAULT_BASE_TOPIC)
    _friendly_name: str | None = field(init=False, default=None)
    _unsubscribe: Callable[[], None] | None = field(init=False, default=None)
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
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes.

        With push updates, we only need polling as a fallback.
        """
        return timedelta(minutes=5)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return interval for hard refresh."""
        return timedelta(hours=1)

    @property
    def connection_check_interval(self) -> timedelta | None:
        """Return interval for connection checks."""
        return timedelta(seconds=30)

    def _get_friendly_name(self) -> str | None:
        """Get the Zigbee2MQTT friendly name for this device."""
        if self._friendly_name is not None:
            return self._friendly_name

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

        # The device name is the friendly_name in Zigbee2MQTT
        self._friendly_name = self.device_entry.name
        LOGGER.debug(
            "Found Zigbee2MQTT friendly name for %s: %s",
            self.lock.entity_id,
            self._friendly_name,
        )
        return self._friendly_name

    def _get_topic(self, suffix: str = "") -> str | None:
        """Get the MQTT topic for this device."""
        friendly_name = self._get_friendly_name()
        if not friendly_name:
            return None
        if suffix:
            return f"{self._base_topic}/{friendly_name}/{suffix}"
        return f"{self._base_topic}/{friendly_name}"

    async def async_is_integration_connected(self) -> bool:
        """Return whether the MQTT integration is connected."""
        if not mqtt_config_entry_enabled(self.hass):
            return False

        # Check if we can get the friendly name (device exists)
        if not self._get_friendly_name():
            return False

        # Check entity state for availability
        state = self.hass.states.get(self.lock.entity_id)
        if state is None or state.state == "unavailable":
            return False

        return True

    @callback
    def _process_z2m_device_payload(self, payload: dict[str, Any]) -> None:
        """Apply device-topic JSON inside the event loop.

        MQTT may invoke subscription callbacks from a worker thread; coordinator and
        asyncio futures are not thread-safe.
        """

        # Handle pin_code added / deleted (Z2M action events, not the users object)
        if payload.get("action") in ("pin_code_added", "pin_code_deleted"):
            action_user = payload.get("action_user")
            if action_user is not None:
                LOGGER.debug(
                    "Lock %s received %s for user %s",
                    self.lock.entity_id,
                    payload.get("action"),
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
            updates: dict[int, str | SlotCode] = {}
            for user_id_str, user_info in users_data.items():
                try:
                    user_id = int(user_id_str)
                except (ValueError, TypeError):
                    continue

                if isinstance(user_info, dict):
                    status = user_info.get("status")
                    pin_code_present = "pin_code" in user_info
                    pin_raw = user_info.get("pin_code")

                    # Zigbee2MQTT often omits pin_code when expose_pin is false (default on
                    # several Yale models). Treating that as EMPTY makes the coordinator think
                    # the slot is cleared, so disabling the slot skips clear_usercode while the
                    # lock still holds the PIN. Only treat as EMPTY when MQTT exposes the field.
                    if status == "enabled":
                        if pin_raw:
                            updates[user_id] = str(pin_raw)
                        elif pin_code_present:
                            updates[user_id] = SlotCode.EMPTY
                        else:
                            continue
                    else:
                        updates[user_id] = SlotCode.EMPTY

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
            user_id = pin_code_data.get("user")
            if user_id is not None:
                try:
                    user_id = int(user_id)
                except (ValueError, TypeError):
                    return

                if user_id in self._pending_codes:
                    future = self._pending_codes.pop(user_id)
                    if not future.done():
                        user_enabled = pin_code_data.get("user_enabled", False)
                        pin_code = pin_code_data.get("pin_code", "")
                        if user_enabled and pin_code:
                            future.set_result(str(pin_code))
                        else:
                            future.set_result(None)

    def setup_push_subscription(self) -> None:
        """Subscribe to MQTT updates for this lock."""
        if self._unsubscribe is not None:
            return  # Already subscribed

        topic = self._get_topic()
        if not topic:
            LOGGER.debug(
                "Cannot subscribe to push updates for %s - no topic",
                self.lock.entity_id,
            )
            raise LockDisconnected(
                f"Cannot subscribe to push updates for {self.lock.entity_id} - no topic"
            )

        async def _async_subscribe():
            if not mqtt_config_entry_enabled(self.hass):
                return

            def message_received(msg: ReceiveMessage) -> None:
                """Handle incoming MQTT message (may run off the event loop)."""
                try:
                    payload = json.loads(msg.payload)
                except (json.JSONDecodeError, TypeError):
                    return

                def _deliver() -> None:
                    self._process_z2m_device_payload(payload)

                self.hass.loop.call_soon_threadsafe(_deliver)

            try:
                self._unsubscribe = await async_subscribe(
                    self.hass, topic, message_received
                )
                LOGGER.debug(
                    "Subscribed to MQTT topic %s for %s", topic, self.lock.entity_id
                )
            except Exception as err:
                LOGGER.error(
                    "Failed to subscribe to MQTT for %s: %s",
                    self.lock.entity_id,
                    err,
                )

        self.hass.async_create_task(_async_subscribe())

    def teardown_push_subscription(self) -> None:
        """Unsubscribe from MQTT updates."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
            LOGGER.debug("Unsubscribed from MQTT for %s", self.lock.entity_id)

        # Cancel any pending futures
        for future in self._pending_codes.values():
            if not future.done():
                future.cancel()
        self._pending_codes.clear()

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Get dictionary of code slots and usercodes."""
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        if not await self.async_is_integration_connected():
            raise LockDisconnected("Lock not connected")

        get_topic = self._get_topic("get")
        if not get_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        # Get configured code slots for this lock
        code_slots = {
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
        }

        data: dict[int, str | SlotCode] = {}

        for slot_num in code_slots:
            try:
                # Create a future to wait for response
                future: asyncio.Future[str | None] = asyncio.Future()
                self._pending_codes[slot_num] = future

                # Request PIN code for this slot
                payload = json.dumps({"pin_code": {"user": slot_num}})
                await async_publish(self.hass, get_topic, payload)

                try:
                    # Wait for response with timeout
                    result = await asyncio.wait_for(future, timeout=10.0)
                    data[slot_num] = result if result else SlotCode.EMPTY
                except TimeoutError:
                    LOGGER.debug(
                        "Timeout waiting for PIN code response for %s slot %s",
                        self.lock.entity_id,
                        slot_num,
                    )
                    data[slot_num] = SlotCode.EMPTY
                finally:
                    self._pending_codes.pop(slot_num, None)

            except Exception as err:
                LOGGER.debug(
                    "Failed to get PIN for %s slot %s: %s",
                    self.lock.entity_id,
                    slot_num,
                    err,
                )
                data[slot_num] = SlotCode.EMPTY

        return data

    async def async_set_usercode(
        self, code_slot: int, usercode: str, name: str | None = None
    ) -> bool:
        """Set a usercode on a code slot."""
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        if not await self.async_is_integration_connected():
            raise LockDisconnected("Lock not connected")

        set_topic = self._get_topic("set")
        if not set_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        try:
            # Zigbee2MQTT set_pin_code payload format
            payload = json.dumps(
                {
                    "pin_code": {
                        "user": code_slot,
                        "user_type": "unrestricted",
                        "pin_code": str(usercode),
                        "user_enabled": True,
                    }
                }
            )

            await async_publish(self.hass, set_topic, payload)
            LOGGER.debug(
                "Published set_pin_code for %s slot %s",
                self.lock.entity_id,
                code_slot,
            )
            # Same pattern as Z-Wave JS: MQTT state updates asynchronously; avoid sync
            # loops by updating the coordinator immediately after a successful publish.
            if self.coordinator:
                self.coordinator.push_update({code_slot: str(usercode)})
            return True

        except Exception as err:
            LOGGER.error(
                "Failed to set PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to set PIN: {err}") from err

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear a usercode on a code slot."""
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        if not await self.async_is_integration_connected():
            raise LockDisconnected("Lock not connected")

        set_topic = self._get_topic("set")
        if not set_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        try:
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

            await async_publish(self.hass, set_topic, payload)
            LOGGER.debug(
                "Published clear_pin_code for %s slot %s",
                self.lock.entity_id,
                code_slot,
            )
            if self.coordinator:
                self.coordinator.push_update({code_slot: SlotCode.EMPTY})
            return True

        except Exception as err:
            LOGGER.error(
                "Failed to clear PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to clear PIN: {err}") from err

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Perform hard refresh and return all codes."""
        return await self.async_get_usercodes()
