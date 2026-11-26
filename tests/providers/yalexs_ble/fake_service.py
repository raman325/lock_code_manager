"""
A stateful Yale Access Bluetooth lock behind the keypad credential actions.

The fake registers ``yalexs_ble.set_lock_credential``,
``yalexs_ble.clear_lock_credential`` and
``yalexs_ble.get_lock_credential_status`` and answers them the way the lock
entity of home-assistant/core#185596 does, over a table of keypad slots that
stands for the lock's:

- ``set_lock_credential`` refuses a PIN another slot holds with a
  ``ServiceValidationError`` (``pin_already_in_use``), as the lock answers
  ``KEYCODE_EXISTING_KEY``; otherwise it programs the slot and stores the
  slot name only when one is given.
- ``clear_lock_credential`` empties the slot and forgets its name.
- ``get_lock_credential_status`` answers ``credential_exists`` and the stored
  name, never the PIN, wrapped per entity as an entity action answers.

``keypad_unlock`` fires ``yalexs_ble_lock_activity`` as the integration does
for a keypad unlock: for the device, with the main lock entity's id.
"""

from dataclasses import dataclass, field

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError

from custom_components.lock_code_manager.providers.yalexs_ble import (
    EVENT_LOCK_ACTIVITY,
    YALEXS_BLE_DOMAIN,
)


@dataclass
class FakeYaleXSBLELock:
    """One lock's keypad slots, behind the integration's credential actions."""

    hass: HomeAssistant
    entity_id: str
    device_id: str | None = None
    # slot -> PIN
    pins: dict[int, str] = field(default_factory=dict)
    # slot -> name, as the integration's credential name store keeps them.
    names: dict[int, str] = field(default_factory=dict)
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def async_install(self) -> None:
        """Register the three actions in place of the integration's."""
        self.hass.config.components.add(YALEXS_BLE_DOMAIN)
        for service, handler, response in (
            ("set_lock_credential", self._async_set, SupportsResponse.NONE),
            ("clear_lock_credential", self._async_clear, SupportsResponse.NONE),
            (
                "get_lock_credential_status",
                self._async_status,
                SupportsResponse.ONLY,
            ),
        ):
            self.hass.services.async_register(
                YALEXS_BLE_DOMAIN, service, handler, supports_response=response
            )

    def count(self, service: str) -> int:
        """Return how many times ``service`` was called."""
        return sum(1 for name, _ in self.calls if name == service)

    def keypad_unlock(self, slot: int) -> None:
        """Fire the activity event of a keypad unlock with ``slot``'s PIN."""
        self.hass.bus.async_fire(
            EVENT_LOCK_ACTIVITY,
            {
                "device_id": self.device_id,
                "entity_id": self.entity_id,
                "source": "pin",
                "slot": slot,
                "master_code": False,
                "name": self.names.get(slot),
            },
        )

    async def _async_set(self, call: ServiceCall) -> None:
        self.calls.append(("set_lock_credential", dict(call.data)))
        slot = call.data["credential_index"]
        pin = call.data["credential_data"]
        if any(held == pin for other, held in self.pins.items() if other != slot):
            raise ServiceValidationError(
                translation_domain=YALEXS_BLE_DOMAIN,
                translation_key="pin_already_in_use",
            )
        self.pins[slot] = pin
        if name := call.data.get("name"):
            self.names[slot] = name

    async def _async_clear(self, call: ServiceCall) -> None:
        self.calls.append(("clear_lock_credential", dict(call.data)))
        slot = call.data["credential_index"]
        self.pins.pop(slot, None)
        self.names.pop(slot, None)

    async def _async_status(self, call: ServiceCall) -> ServiceResponse:
        self.calls.append(("get_lock_credential_status", dict(call.data)))
        slot = call.data["credential_index"]
        exists = slot in self.pins
        return {
            self.entity_id: {
                "credential_exists": exists,
                "name": self.names.get(slot) if exists else None,
            }
        }
