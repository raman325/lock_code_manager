"""Tests for the BaseLock User->Credential seam and orchestration (PR 3)."""

from __future__ import annotations

from typing import Literal

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    User,
    credential_from_slot,
    user_from_slot,
)
from custom_components.lock_code_manager.domain.exceptions import (
    ProviderNotImplementedError,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers._base import BaseLock


def _make_lock(hass: HomeAssistant, cls: type[BaseLock], unique: str) -> BaseLock:
    """Build a provider instance wired to a registry entry, no coordinator."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", unique, config_entry=config_entry
    )
    return cls(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)


class _NativeStubLock(BaseLock):
    """Synthetic native-user provider: records primitive calls, in-memory users."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple] = []
        self._users: dict[int, User] = {}

    @property
    def domain(self) -> str:
        return "test"

    @property
    def supports_native_users(self) -> bool:
        return True

    async def _set_user(self, user: User) -> int:
        self.calls.append(("set_user", user.user_id, user.name))
        self._users[user.user_id] = User(
            user_id=user.user_id, name=user.name, active=user.active
        )
        return user.user_id

    async def _delete_user(self, user_id: int) -> None:
        self.calls.append(("delete_user", user_id))
        self._users.pop(user_id, None)

    async def _set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        self.calls.append(("set_credential", user_id, credential.slot))
        self._users[user_id].credentials = [credential]
        return True

    async def _delete_credential(self, ref: CredentialRef) -> bool:
        self.calls.append(("delete_credential", ref.user_id, ref.slot))
        user = self._users.get(ref.user_id)
        if user is None:
            return False
        user.credentials = []
        return True

    async def _get_users(self) -> list[User]:
        return list(self._users.values())


class _DegenerateStubLock(BaseLock):
    """Synthetic slot-only provider: implements only credential primitives."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple] = []
        self._slots: dict[int, SlotCredential] = {}

    @property
    def domain(self) -> str:
        return "test"

    async def _set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        self.calls.append(("set_credential", user_id, credential.slot))
        self._slots[credential.slot] = credential.state
        return True

    async def _delete_credential(self, ref: CredentialRef) -> bool:
        self.calls.append(("delete_credential", ref.user_id, ref.slot))
        return self._slots.pop(ref.slot, None) is not None

    async def _get_users(self) -> list[User]:
        return [user_from_slot(slot, state) for slot, state in self._slots.items()]


async def test_supports_native_users_defaults_false(hass: HomeAssistant) -> None:
    """The flag is False unless a provider opts in."""
    lock = _make_lock(hass, _DegenerateStubLock, "seam_flag")
    assert lock.supports_native_users is False


async def test_primitive_defaults_raise(hass: HomeAssistant) -> None:
    """A bare BaseLock provides no primitives; each default raises."""
    lock = _make_lock(hass, BaseLock, "seam_defaults")
    with pytest.raises(ProviderNotImplementedError):
        await lock._set_user(User(user_id=1))
    with pytest.raises(ProviderNotImplementedError):
        await lock._delete_user(1)
    with pytest.raises(ProviderNotImplementedError):
        await lock._set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1")),
            name=None,
            source="direct",
        )
    with pytest.raises(ProviderNotImplementedError):
        await lock._delete_credential(CredentialRef(1, CredentialType.PIN, 1))
    with pytest.raises(ProviderNotImplementedError):
        await lock._get_users()
