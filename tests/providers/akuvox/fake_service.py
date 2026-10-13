"""
A stateful Akuvox device behind the ``local_akuvox`` integration's user services.

The fake registers ``local_akuvox.list_users``, ``add_user``, ``modify_user``
and ``delete_user`` and answers them the way that integration's lock entity
does, over a table of the device's users. References, read at:

- homeassistant-local-akuvox ``bfa05b5a`` (the ``local_akuvox`` domain):
  ``AL`` = custom_components/local_akuvox/lock.py, ``AV`` =
  custom_components/local_akuvox/validation.py.
- pylocal-akuvox ``fa1af306``: ``PU`` = src/pylocal_akuvox/models/users.py.

What it answers:

- ``list_users`` returns ``{"users": [vars(user), ...]}`` (AL:872-916), each
  user carrying ``User``'s fields (PU:19-32), wrapped per entity as an entity
  service answers.
- ``add_user`` checks the PIN is 4-8 digits (AV:53-66), builds the
  schedule-relay string from the schedules and the lock's relay
  (AV:175-192), and stores the user with a ``user_id`` of the current time
  when none is given (AL:1258-1305). The device assigns ``id``.
- ``modify_user`` and ``delete_user`` look the user up by ``id``: one not
  there is ``HomeAssistantError("<service>: user '<id>' not found")``, one
  the cloud provisioned a ``ServiceValidationError`` (AL:1159-1209); then
  they change or remove it (AL:1307-1406).

``ignore`` makes a write acknowledged and not applied, as a device that
drops it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.lock_code_manager.providers.akuvox import AKUVOX_DOMAIN

# ``User``'s fields, in the order the library declares them (PU:19-32).
_USER_FIELDS = (
    "name",
    "user_id",
    "schedule_relay",
    "id",
    "web_relay",
    "private_pin",
    "card_code",
    "lift_floor_num",
    "user_type",
    "source",
    "source_type",
)


@dataclass
class FakeAkuvoxDevice:
    """The users one Akuvox device holds, behind the integration's user services."""

    hass: HomeAssistant
    entity_id: str
    # The relay the lock entity drives (AL:396), 1-based.
    relay_number: int = 1
    users: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    _ignored: set[str] = field(default_factory=set)
    _next_id: int = 1

    def install(self) -> None:
        """Register the four user services in place of the integration's."""
        for service, handler in (
            ("list_users", self._async_list_users),
            ("add_user", self._async_add_user),
            ("modify_user", self._async_modify_user),
            ("delete_user", self._async_delete_user),
        ):
            self.hass.services.async_register(
                AKUVOX_DOMAIN,
                service,
                handler,
                supports_response=SupportsResponse.OPTIONAL,
            )

    def add(self, name: str, pin: str | None, **fields: Any) -> str:
        """Put a user on the device, as its web interface would; return its id."""
        user = dict.fromkeys(_USER_FIELDS)
        # A user made on the device itself, as A08S/E18C firmware marks one:
        # source_type "1" (local), user_type "0" (the provider's own notes,
        # providers/akuvox.py:49-53; the integration's cloud test, AV:69-89).
        user |= {
            "name": name,
            "user_id": str(int(time.time())),
            "schedule_relay": f"1001-{self.relay_number}",
            "private_pin": pin,
            "user_type": "0",
            "source_type": "1",
        }
        user |= fields
        user["id"] = str(self._next_id)
        self._next_id += 1
        self.users.append(user)
        return user["id"]

    def pin_of(self, name: str) -> str | None:
        """Return the PIN of the user named ``name``, if the device has one."""
        return next((u["private_pin"] for u in self.users if u["name"] == name), None)

    def ignore(self, service: str) -> None:
        """Acknowledge ``service`` calls without applying them."""
        self._ignored.add(service)

    def count(self, service: str) -> int:
        """Return how many times ``service`` was called."""
        return sum(name == service for name, _ in self.calls)

    # Services -------------------------------------------------------------

    def _find(self, user_id: str, service: str) -> dict[str, Any]:
        """Look a user up by device id, as AL:1159-1209 does."""
        user = next((u for u in self.users if u["id"] == user_id), None)
        if user is None:
            raise HomeAssistantError(f"{service}: user '{user_id}' not found")
        if _cloud_provisioned(user):
            raise ServiceValidationError(f"{service}: user is cloud-provisioned")
        return user

    async def _async_list_users(self, call: ServiceCall) -> ServiceResponse:
        self.calls.append(("list_users", dict(call.data)))
        return {self.entity_id: {"users": [dict(u) for u in self.users]}}

    async def _async_add_user(self, call: ServiceCall) -> None:
        data = dict(call.data)
        self.calls.append(("add_user", data))
        _validate_pin(data.get("private_pin"))
        if "add_user" in self._ignored:
            return
        self.add(
            data["name"],
            data.get("private_pin"),
            user_id=data.get("user_id") or str(int(time.time())),
            schedule_relay=",".join(
                f"{display_id}-{self.relay_number}" for display_id in data["schedules"]
            ),
            lift_floor_num=data["lift_floor_num"],
            web_relay=data.get("web_relay"),
            card_code=data.get("card_code"),
        )

    async def _async_modify_user(self, call: ServiceCall) -> None:
        data = dict(call.data)
        self.calls.append(("modify_user", data))
        user = self._find(data["id"], "modify_user")
        _validate_pin(data.get("private_pin"))
        if "modify_user" in self._ignored:
            return
        for key in (
            "name",
            "user_id",
            "lift_floor_num",
            "web_relay",
            "private_pin",
            "card_code",
        ):
            if data.get(key) is not None:
                user[key] = data[key]

    async def _async_delete_user(self, call: ServiceCall) -> None:
        data = dict(call.data)
        self.calls.append(("delete_user", data))
        user = self._find(data["id"], "delete_user")
        if "delete_user" in self._ignored:
            return
        self.users.remove(user)


def _cloud_provisioned(user: dict[str, Any]) -> bool:
    """Return whether the cloud provisioned ``user`` (AV:69-89)."""
    if user.get("source") is not None and user["source"] not in ("Local", ""):
        return True
    return user.get("source_type") is not None and user["source_type"] not in (
        "1",
        "Local",
        "",
    )


def _validate_pin(pin: str | None) -> None:
    """Refuse a PIN that is not 4-8 digits, when one is given (AV:53-66)."""
    if pin is not None and (len(pin) < 4 or len(pin) > 8 or not pin.isdigit()):
        raise ServiceValidationError("PIN must be 4-8 digits")
