"""Tests for the BaseLock User->Credential seam and orchestration (PR 3)."""

from __future__ import annotations

from typing import Literal
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    SetUserResult,
    User,
    credential_from_slot,
    user_from_slot,
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    LockOperationFailed,
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
        self.last_set_credential: dict | None = None
        self._users: dict[int, User] = {}

    @property
    def domain(self) -> str:
        return "test"

    @property
    def supports_native_users(self) -> bool:
        return True

    async def async_set_user(self, user: User) -> SetUserResult:
        created = user.user_id not in self._users
        self.calls.append(("set_user", user.user_id, user.name))
        self._users[user.user_id] = User(
            user_id=user.user_id, name=user.name, active=user.active
        )
        return SetUserResult(user_id=user.user_id, created=created)

    async def async_delete_user(self, user_id: int) -> None:
        self.calls.append(("delete_user", user_id))
        self._users.pop(user_id, None)

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        self.calls.append(("set_credential", user_id, credential.slot))
        self.last_set_credential = {
            "user_id": user_id,
            "slot": credential.slot,
            "name": name,
            "source": source,
        }
        self._users[user_id].credentials = [credential]
        return True

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        self.calls.append(("delete_credential", ref.user_id, ref.slot))
        user = self._users.get(ref.user_id)
        if user is None:
            return False
        user.credentials = []
        return True

    async def async_get_users(self) -> list[User]:
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

    async def async_set_credential(
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

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        self.calls.append(("delete_credential", ref.user_id, ref.slot))
        return self._slots.pop(ref.slot, None) is not None

    async def async_get_users(self) -> list[User]:
        return [user_from_slot(slot, state) for slot, state in self._slots.items()]


async def test_supports_native_users_defaults_false(hass: HomeAssistant) -> None:
    """The flag is False unless a provider opts in."""
    lock = _make_lock(hass, _DegenerateStubLock, "seam_flag")
    assert lock.supports_native_users is False


async def test_primitive_defaults_raise(hass: HomeAssistant) -> None:
    """A bare BaseLock provides no primitives; each default raises."""
    lock = _make_lock(hass, BaseLock, "seam_defaults")
    with pytest.raises(ProviderNotImplementedError):
        await lock.async_set_user(User(user_id=1))
    with pytest.raises(ProviderNotImplementedError):
        await lock.async_delete_user(1)
    with pytest.raises(ProviderNotImplementedError):
        await lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1")),
            name=None,
            source="direct",
        )
    with pytest.raises(ProviderNotImplementedError):
        await lock.async_delete_credential(CredentialRef(1, CredentialType.PIN, 1))
    with pytest.raises(ProviderNotImplementedError):
        await lock.async_get_users()


async def test_get_usercodes_projects_pin_credentials(hass: HomeAssistant) -> None:
    """async_get_usercodes flattens users' PIN credentials to slot -> state."""
    lock = _make_lock(hass, _NativeStubLock, "seam_get")
    lock._users = {
        1: User(
            user_id=1,
            credentials=[credential_from_slot(1, SlotCredential.known("1234"))],
        ),
        2: User(
            user_id=2,
            credentials=[credential_from_slot(2, SlotCredential.unreadable())],
        ),
        5: User(
            user_id=5,
            credentials=[credential_from_slot(5, SlotCredential.empty())],
        ),
    }
    assert await lock.async_get_usercodes() == {
        1: SlotCredential.known("1234"),
        2: SlotCredential.unreadable(),
        5: SlotCredential.empty(),
    }


async def test_get_usercodes_includes_empty_managed_slots(hass: HomeAssistant) -> None:
    """Managed slots are present as empty even when no user owns them."""
    lock = _make_lock(hass, _NativeStubLock, "seam_get_managed")
    lock._users = {
        1: User(
            user_id=1,
            credentials=[credential_from_slot(1, SlotCredential.known("1234"))],
        )
    }
    with patch(
        "custom_components.lock_code_manager.providers._base.get_managed_slots",
        return_value={1, 2, 3},
    ):
        result = await lock.async_get_usercodes()
    assert result == {
        1: SlotCredential.known("1234"),  # managed and occupied
        2: SlotCredential.empty(),  # managed, empty placeholder
        3: SlotCredential.empty(),  # managed, empty placeholder
    }


async def test_get_usercodes_surfaces_unmanaged_occupied_slots(
    hass: HomeAssistant,
) -> None:
    """A slot the lock reports but LCM does not manage is still surfaced."""
    lock = _make_lock(hass, _NativeStubLock, "seam_get_unmanaged")
    lock._users = {
        9: User(
            user_id=9,
            credentials=[credential_from_slot(9, SlotCredential.unreadable())],
        )
    }
    with patch(
        "custom_components.lock_code_manager.providers._base.get_managed_slots",
        return_value={1},
    ):
        result = await lock.async_get_usercodes()
    assert result == {
        1: SlotCredential.empty(),  # managed, empty
        9: SlotCredential.unreadable(),  # unmanaged but occupied
    }


async def test_get_usercodes_drops_non_pin_credentials(hass: HomeAssistant) -> None:
    """Only PIN credentials project to slots this round."""
    lock = _make_lock(hass, _NativeStubLock, "seam_get_nonpin")
    lock._users = {
        1: User(
            user_id=1,
            credentials=[
                credential_from_slot(1, SlotCredential.known("1234")),
                Credential(
                    type=CredentialType.RFID, slot=1, state=SlotCredential.unreadable()
                ),
            ],
        )
    }
    assert await lock.async_get_usercodes() == {1: SlotCredential.known("1234")}


async def test_set_usercode_native_user_first_and_threads_id(
    hass: HomeAssistant,
) -> None:
    """Native set creates the user first, then its credential, threading the id."""
    lock = _make_lock(hass, _NativeStubLock, "seam_set_native")
    changed = await lock.async_set_usercode(3, "9999", name="alice")
    assert changed is True
    assert lock.calls == [
        ("set_user", 3, "alice"),
        ("set_credential", 3, 3),
    ]
    assert lock._users[3].credentials[0].matches("9999")


async def test_set_usercode_degenerate_skips_user(hass: HomeAssistant) -> None:
    """Slot-only set writes the credential directly, no user operation."""
    lock = _make_lock(hass, _DegenerateStubLock, "seam_set_degen")
    changed = await lock.async_set_usercode(3, "9999")
    assert changed is True
    assert lock.calls == [("set_credential", 3, 3)]
    assert lock._slots[3].matches("9999")


async def test_clear_usercode_native_deletes_user_on_last_credential(
    hass: HomeAssistant,
) -> None:
    """Native clear removes the credential then the now-empty user."""
    lock = _make_lock(hass, _NativeStubLock, "seam_clear_native")
    await lock.async_set_usercode(3, "9999", name="alice")
    lock.calls.clear()
    changed = await lock.async_clear_usercode(3)
    assert changed is True
    assert lock.calls == [
        ("delete_credential", 3, 3),
        ("delete_user", 3),
    ]
    assert 3 not in lock._users


async def test_clear_usercode_degenerate_no_user_op(hass: HomeAssistant) -> None:
    """Slot-only clear deletes the credential and performs no user operation."""
    lock = _make_lock(hass, _DegenerateStubLock, "seam_clear_degen")
    await lock.async_set_usercode(3, "9999")
    lock.calls.clear()
    changed = await lock.async_clear_usercode(3)
    assert changed is True
    assert lock.calls == [("delete_credential", 3, 3)]


async def test_clear_usercode_degenerate_returns_false_when_absent(
    hass: HomeAssistant,
) -> None:
    """Clearing an empty slot reports no change."""
    lock = _make_lock(hass, _DegenerateStubLock, "seam_clear_absent")
    assert await lock.async_clear_usercode(7) is False
    assert lock.calls == [("delete_credential", 7, 7)]


async def test_clear_usercode_native_no_op_when_no_owner(
    hass: HomeAssistant,
) -> None:
    """Native clear of a slot no user owns issues no delete at all."""
    lock = _make_lock(hass, _NativeStubLock, "seam_clear_native_absent")
    changed = await lock.async_clear_usercode(7)
    assert changed is False
    assert lock.calls == []
    assert 7 not in lock._users


async def test_clear_usercode_native_resolves_owner_when_user_id_not_slot(
    hass: HomeAssistant,
) -> None:
    """Native clear targets the credential's real owner, not the slot index."""
    lock = _make_lock(hass, _NativeStubLock, "seam_clear_foreign_owner")
    # A user whose id differs from the credential slot (e.g. created by another
    # controller or auto-allocated by the integration).
    lock._users = {
        12: User(
            user_id=12,
            name="bob",
            credentials=[credential_from_slot(5, SlotCredential.known("1234"))],
        )
    }
    changed = await lock.async_clear_usercode(5)
    assert changed is True
    assert lock.calls == [
        ("delete_credential", 12, 5),
        ("delete_user", 12),
    ]
    assert 12 not in lock._users


async def test_clear_usercode_native_keeps_user_with_other_credentials(
    hass: HomeAssistant,
) -> None:
    """Native clear deletes only the credential when the user has others."""
    lock = _make_lock(hass, _NativeStubLock, "seam_clear_multi_cred")
    lock._users = {
        4: User(
            user_id=4,
            credentials=[
                credential_from_slot(4, SlotCredential.known("1234")),
                credential_from_slot(8, SlotCredential.known("5678")),
            ],
        )
    }
    changed = await lock.async_clear_usercode(4)
    assert changed is True
    assert lock.calls == [("delete_credential", 4, 4)]
    assert ("delete_user", 4) not in lock.calls


async def test_internal_set_usercode_drives_orchestration(
    hass: HomeAssistant,
) -> None:
    """The external set wrapper routes through the orchestration to primitives."""
    lock = _make_lock(hass, _NativeStubLock, "seam_internal_set")
    lock._min_operation_delay = 0.0
    # async_is_device_available is intentionally left at its default (True);
    # only the connection gate needs forcing to reach the orchestration.
    with patch.object(BaseLock, "async_is_integration_connected", return_value=True):
        await lock.async_internal_set_usercode(4, "4321", "carol")
    assert ("set_user", 4, "carol") in lock.calls
    assert ("set_credential", 4, 4) in lock.calls


async def test_internal_roundtrip_set_get_clear(hass: HomeAssistant) -> None:
    """Set then get then clear round-trips through the seam end to end."""
    lock = _make_lock(hass, _NativeStubLock, "seam_internal_roundtrip")
    lock._min_operation_delay = 0.0
    with patch.object(BaseLock, "async_is_integration_connected", return_value=True):
        await lock.async_internal_set_usercode(1, "1111", "a")
        await lock.async_internal_set_usercode(2, "2222", "b")
        assert await lock.async_internal_get_usercodes() == {
            1: SlotCredential.known("1111"),
            2: SlotCredential.known("2222"),
        }
        await lock.async_internal_clear_usercode(1)
        assert await lock.async_internal_get_usercodes() == {
            2: SlotCredential.known("2222")
        }


async def test_set_usercode_threads_name_and_source(hass: HomeAssistant) -> None:
    """name and source flow through the orchestration into _set_credential."""
    lock = _make_lock(hass, _NativeStubLock, "seam_thread_kwargs")
    await lock.async_set_usercode(2, "2468", name=None, source="sync")
    assert lock.last_set_credential == {
        "user_id": 2,
        "slot": 2,
        "name": None,
        "source": "sync",
    }


class _CredentialWriteFailsLock(_NativeStubLock):
    """Native stub whose credential write always fails."""

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        self.calls.append(("set_credential", user_id, credential.slot))
        raise CodeRejectedError(
            code_slot=credential.slot, lock_entity_id=self.lock.entity_id
        )


async def test_set_usercode_rolls_back_newly_created_user_on_failure(
    hass: HomeAssistant,
) -> None:
    """A failed credential write deletes the user this set just created."""
    lock = _make_lock(hass, _CredentialWriteFailsLock, "seam_rollback_created")
    with pytest.raises(CodeRejectedError):
        await lock.async_set_usercode(3, "9999", name="alice")
    assert lock.calls == [
        ("set_user", 3, "alice"),
        ("set_credential", 3, 3),
        ("delete_user", 3),
    ]
    assert 3 not in lock._users


async def test_set_usercode_keeps_pre_existing_user_on_failure(
    hass: HomeAssistant,
) -> None:
    """A failed credential write does not delete a user that already existed."""
    lock = _make_lock(hass, _CredentialWriteFailsLock, "seam_rollback_existing")
    lock._users = {
        3: User(
            user_id=3,
            name="bob",
            credentials=[credential_from_slot(3, SlotCredential.known("0000"))],
        )
    }
    with pytest.raises(CodeRejectedError):
        await lock.async_set_usercode(3, "9999", name="bob")
    assert ("delete_user", 3) not in lock.calls
    assert 3 in lock._users


class _UserDeleteFailsLock(_NativeStubLock):
    """Native stub whose user delete always fails."""

    async def async_delete_user(self, user_id: int) -> None:
        self.calls.append(("delete_user", user_id))
        raise LockOperationFailed("user delete failed")


async def test_clear_usercode_tolerates_user_delete_failure(
    hass: HomeAssistant,
) -> None:
    """Clear still reports the credential change when the user delete fails."""
    lock = _make_lock(hass, _UserDeleteFailsLock, "seam_clear_deluser_fail")
    lock._users = {
        4: User(
            user_id=4,
            credentials=[credential_from_slot(4, SlotCredential.known("1234"))],
        )
    }
    changed = await lock.async_clear_usercode(4)
    assert changed is True
    assert ("delete_credential", 4, 4) in lock.calls
    assert ("delete_user", 4) in lock.calls
