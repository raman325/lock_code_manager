"""Tests for the BaseLock User->Credential seam and orchestration (PR 3)."""

from __future__ import annotations

from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    SetUserResult,
    User,
    credential_from_slot,
    user_from_slot,
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    LockCodeManagerProviderError,
    LockDisconnected,
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
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        self.calls.append(("set_credential", user_id, credential.slot))
        self.last_set_credential = {
            "user_id": user_id,
            "slot": credential.slot,
            "pin": pin,
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

    async def async_get_capabilities(self) -> LockCapabilities:
        # Real user records with names available — this is what gates
        # ``_set_credential`` into the set-user-first path.
        return LockCapabilities(
            supports_user_management=True,
            max_users=30,
            credential_types={
                CredentialType.PIN: CredentialTypeCapability(
                    num_slots=30,
                    min_length=4,
                    max_length=8,
                    supports_learn=False,
                ),
            },
            max_user_name_length=16,
        )


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
        pin: str,
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
            "1",
            name=None,
            source="direct",
        )
    with pytest.raises(ProviderNotImplementedError):
        await lock.async_delete_credential(CredentialRef(1, CredentialType.PIN, 1))
    with pytest.raises(ProviderNotImplementedError):
        await lock.async_get_users()


async def test_setup_internal_rejects_lock_without_pin_support(
    hass: HomeAssistant,
) -> None:
    """A native-user lock missing PIN support fails setup with a typed error."""

    class _NoPinLock(_NativeStubLock):
        async def async_get_capabilities(self) -> LockCapabilities:
            return LockCapabilities(
                supports_user_management=True,
                max_users=30,
                credential_types={},  # no PIN
                max_user_name_length=16,
            )

    lock = _make_lock(hass, _NoPinLock, "seam_setup_no_pin")
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    with pytest.raises(LockCodeManagerProviderError, match="PIN credential"):
        await lock.async_setup_internal(config_entry)


async def test_setup_internal_rejects_lock_without_user_management(
    hass: HomeAssistant,
) -> None:
    """A native-user lock missing user management fails setup with a typed error."""

    class _NoUserMgmtLock(_NativeStubLock):
        async def async_get_capabilities(self) -> LockCapabilities:
            return LockCapabilities(
                supports_user_management=False,
                max_users=30,
                credential_types={
                    CredentialType.PIN: CredentialTypeCapability(
                        num_slots=30,
                        min_length=4,
                        max_length=8,
                        supports_learn=False,
                    ),
                },
                max_user_name_length=16,
            )

    lock = _make_lock(hass, _NoUserMgmtLock, "seam_setup_no_user_mgmt")
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    with pytest.raises(LockCodeManagerProviderError, match="user management"):
        await lock.async_setup_internal(config_entry)


async def test_setup_internal_skips_capability_check_for_slot_only_providers(
    hass: HomeAssistant,
) -> None:
    """Slot-only providers don't have capabilities; base must not call them."""
    lock = _make_lock(hass, _DegenerateStubLock, "seam_setup_degen")
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    # The default async_get_capabilities raises ProviderNotImplementedError;
    # if the base attempted to read caps, that error would propagate. The
    # gate on supports_native_users keeps it from running.
    await lock.async_setup_internal(config_entry)


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


async def test_project_users_to_slots_is_type_parametric(
    hass: HomeAssistant,
) -> None:
    """
    The base projection helper isolates one credential type per call.

    Option A wiring: ``async_get_usercodes`` is a thin Personal Identification
    Number-shaped wrapper over ``_project_users_to_slots``; calling the helper
    with a different ``CredentialType`` returns the slot view for that type.
    Regression guard for the chokepoint -- if a future caller needs the
    Radio Frequency Identification view, it goes through this same code path
    rather than a parallel projection.
    """
    lock = _make_lock(hass, _NativeStubLock, "seam_project_typed")
    lock._users = {
        1: User(
            user_id=1,
            credentials=[
                credential_from_slot(1, SlotCredential.known("1234")),
                Credential(
                    type=CredentialType.RFID,
                    slot=2,
                    state=SlotCredential.unreadable(),
                ),
                Credential(
                    type=CredentialType.PASSWORD,
                    slot=3,
                    state=SlotCredential.known("hunter2"),
                ),
            ],
        ),
    }

    with patch(
        "custom_components.lock_code_manager.providers._base.get_managed_slots",
        return_value={1, 2, 3},
    ):
        pin_slots = await lock._project_users_to_slots(CredentialType.PIN)
        rfid_slots = await lock._project_users_to_slots(CredentialType.RFID)
        password_slots = await lock._project_users_to_slots(CredentialType.PASSWORD)
        # async_get_usercodes() is the PIN wrapper.
        assert await lock.async_get_usercodes() == pin_slots

    # Each projection only sees its own credential type; the managed-slot
    # empty placeholders persist across all of them.
    assert pin_slots == {
        1: SlotCredential.known("1234"),
        2: SlotCredential.empty(),
        3: SlotCredential.empty(),
    }
    assert rfid_slots == {
        1: SlotCredential.empty(),
        2: SlotCredential.unreadable(),
        3: SlotCredential.empty(),
    }
    assert password_slots == {
        1: SlotCredential.empty(),
        2: SlotCredential.empty(),
        3: SlotCredential.known("hunter2"),
    }


# ──────────────────────────────────────────────────────────────────────
# Base capability-derived helpers
# ──────────────────────────────────────────────────────────────────────


def _caps(
    *, supports_user_management: bool, max_user_name_length: int
) -> LockCapabilities:
    return LockCapabilities(
        supports_user_management=supports_user_management,
        max_users=30,
        credential_types={
            CredentialType.PIN: CredentialTypeCapability(
                num_slots=30,
                min_length=4,
                max_length=8,
                supports_learn=False,
            ),
        },
        max_user_name_length=max_user_name_length,
    )


async def test_supports_user_records_true_when_management_and_named(
    hass: HomeAssistant,
) -> None:
    """``supports_user_management AND max_user_name_length > 0`` → True."""
    lock = _make_lock(hass, _NativeStubLock, "seam_supports_users_true")
    lock._capabilities_cache = _caps(
        supports_user_management=True, max_user_name_length=16
    )
    assert await lock._supports_user_records() is True


async def test_supports_user_records_false_when_max_name_length_zero(
    hass: HomeAssistant,
) -> None:
    """Implicit-user lock (e.g. Z-Wave User Code CC) → False."""
    lock = _make_lock(hass, _NativeStubLock, "seam_supports_users_uc")
    lock._capabilities_cache = _caps(
        supports_user_management=True, max_user_name_length=0
    )
    assert await lock._supports_user_records() is False


async def test_supports_user_records_false_when_management_disabled(
    hass: HomeAssistant,
) -> None:
    """A lock that doesn't expose user management → False."""
    lock = _make_lock(hass, _NativeStubLock, "seam_supports_users_no_mgmt")
    lock._capabilities_cache = _caps(
        supports_user_management=False, max_user_name_length=16
    )
    assert await lock._supports_user_records() is False


async def test_build_tagged_user_name_within_limit_keeps_full_display(
    hass: HomeAssistant,
) -> None:
    """``lcm:<slot>:<display>`` fits the lock's limit -- everything stays."""
    lock = _make_lock(hass, _NativeStubLock, "seam_tag_within")
    lock._capabilities_cache = _caps(
        supports_user_management=True, max_user_name_length=16
    )
    assert await lock._build_tagged_user_name(5, "alice") == "lcm:5:alice"


async def test_build_tagged_user_name_truncates_only_display(
    hass: HomeAssistant,
) -> None:
    """When the total exceeds the limit, only the display portion is cut."""
    lock = _make_lock(hass, _NativeStubLock, "seam_tag_truncate")
    # "lcm:5:" is 6 chars, limit 10 → 4 chars of display ("alex").
    lock._capabilities_cache = _caps(
        supports_user_management=True, max_user_name_length=10
    )
    assert await lock._build_tagged_user_name(5, "alexandra") == "lcm:5:alex"


async def test_build_tagged_user_name_handles_none_display(
    hass: HomeAssistant,
) -> None:
    """A ``None`` display becomes the synthetic ``Code Slot <n>`` placeholder."""
    lock = _make_lock(hass, _NativeStubLock, "seam_tag_none")
    lock._capabilities_cache = _caps(
        supports_user_management=True, max_user_name_length=32
    )
    # make_tagged_name uses "Code Slot <n>" when display is falsy.
    assert await lock._build_tagged_user_name(5, None) == "lcm:5:Code Slot 5"


async def test_build_tagged_user_name_returns_none_when_max_zero(
    hass: HomeAssistant,
) -> None:
    """Locks without named users return None so the seam skips the user write."""
    lock = _make_lock(hass, _NativeStubLock, "seam_tag_zero")
    lock._capabilities_cache = _caps(
        supports_user_management=True, max_user_name_length=0
    )
    assert await lock._build_tagged_user_name(5, "alice") is None


async def test_build_tagged_user_name_truncates_prefix_when_limit_below_overhead(
    hass: HomeAssistant,
) -> None:
    """If even the prefix doesn't fit, return the prefix truncated to the limit.

    Pathological case -- a lock advertising ``max_user_name_length`` below
    the prefix length (``lcm:255:`` = 8 chars) means we cannot encode the
    slot recoverably. The helper still returns something rather than
    crashing the write; the write path eats a degraded name.
    """
    lock = _make_lock(hass, _NativeStubLock, "seam_tag_under")
    lock._capabilities_cache = _caps(
        supports_user_management=True, max_user_name_length=3
    )
    assert await lock._build_tagged_user_name(255, "alice") == "lcm"


async def test_build_tagged_user_name_returns_none_when_caps_fetch_fails(
    hass: HomeAssistant,
) -> None:
    """Caps-read failure falls back to None instead of blocking the write."""
    lock = _make_lock(hass, _NativeStubLock, "seam_tag_fail")
    with patch.object(
        type(lock),
        "async_get_capabilities",
        AsyncMock(side_effect=LockDisconnected("unreachable")),
    ):
        assert await lock._build_tagged_user_name(5, "alice") is None
    # Cache stays unset so the next call retries.
    assert lock._capabilities_cache is None


async def test_assert_credential_type_supported_accepts_advertised(
    hass: HomeAssistant,
) -> None:
    """PIN is advertised by the stub → no raise."""
    lock = _make_lock(hass, _NativeStubLock, "seam_assert_type_ok")
    credential = credential_from_slot(1, SlotCredential.known("1234"))
    # Should not raise.
    await lock._assert_credential_type_supported(credential)


async def test_assert_credential_type_supported_rejects_unadvertised(
    hass: HomeAssistant,
) -> None:
    """A type the lock doesn't advertise → CodeRejectedError."""
    lock = _make_lock(hass, _NativeStubLock, "seam_assert_type_reject")
    credential = Credential(
        type=CredentialType.RFID, slot=4, state=SlotCredential.known("AABB")
    )
    with pytest.raises(CodeRejectedError) as exc_info:
        await lock._assert_credential_type_supported(credential)
    assert exc_info.value.code_slot == 4
    assert "unsupported credential type" in str(exc_info.value)


async def test_assert_credential_ref_supported_rejects_unadvertised(
    hass: HomeAssistant,
) -> None:
    """A ref of a type the lock doesn't advertise → CodeRejectedError."""
    lock = _make_lock(hass, _NativeStubLock, "seam_assert_ref_reject")
    ref = CredentialRef(user_id=1, type=CredentialType.RFID, slot=7)
    with pytest.raises(CodeRejectedError) as exc_info:
        await lock._assert_credential_ref_supported(ref)
    assert exc_info.value.code_slot == 7
    assert "unsupported credential type" in str(exc_info.value)


async def test_set_credential_rejects_unsupported_credential_type(
    hass: HomeAssistant,
) -> None:
    """``_set_credential`` asserts the type before any set call."""
    lock = _make_lock(hass, _NativeStubLock, "seam_put_reject_type")
    rfid_credential = Credential(
        type=CredentialType.RFID, slot=2, state=SlotCredential.known("AABB")
    )
    user = User(user_id=2, credentials=[rfid_credential])
    with pytest.raises(CodeRejectedError):
        await lock._set_credential(
            user, rfid_credential, "AABB", name=None, source="direct"
        )
    # No primitive was reached because the assertion fired first.
    assert lock.calls == []


async def test_delete_credential_rejects_unsupported_credential_type(
    hass: HomeAssistant,
) -> None:
    """``_delete_credential`` asserts the ref's type before any delete call."""
    lock = _make_lock(hass, _NativeStubLock, "seam_drop_reject_type")
    ref = CredentialRef(user_id=1, type=CredentialType.RFID, slot=3)
    with pytest.raises(CodeRejectedError):
        await lock._delete_credential(ref)
    assert lock.calls == []


async def test_require_readable_pin_rejects_unreadable_credential(
    hass: HomeAssistant,
) -> None:
    """The base contract helper raises before any provider primitive is reached.

    The seam (``async_set_usercode``) constructs Known credentials so this
    branch is unreachable in production; the helper exists to express the
    invariant once and give providers a guaranteed-string ``pin`` argument.
    A direct call here pins that contract.
    """
    lock = _make_lock(hass, _NativeStubLock, "seam_require_pin")
    unreadable = Credential(
        type=CredentialType.PIN, slot=4, state=SlotCredential.unreadable()
    )
    with pytest.raises(CodeRejectedError) as exc_info:
        lock._require_readable_pin(unreadable)
    assert exc_info.value.code_slot == 4


async def test_set_usercode_native_user_first_and_threads_id(
    hass: HomeAssistant,
) -> None:
    """Native set creates the user first, then its credential, threading the id."""
    lock = _make_lock(hass, _NativeStubLock, "seam_set_native")
    changed = await lock.async_set_usercode(3, "9999", name="alice")
    assert changed is True
    assert lock.calls == [
        ("set_user", 3, "lcm:3:alice"),
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


async def test_clear_usercode_native_preserves_user_record(
    hass: HomeAssistant,
) -> None:
    """Native clear removes the credential and leaves the slot's user in place.

    The lock-side user is now an LCM-managed slot anchor: it persists for
    the slot's whole lifetime so the slot keeps the same identity across
    PIN cycles. Teardown happens only when the slot itself is removed
    from LCM config (see ``async_release_managed_slot``).
    """
    lock = _make_lock(hass, _NativeStubLock, "seam_clear_native")
    await lock.async_set_usercode(3, "9999", name="alice")
    lock.calls.clear()
    changed = await lock.async_clear_usercode(3)
    assert changed is True
    assert lock.calls == [("delete_credential", 3, 3)]
    assert 3 in lock._users


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
    # The ref is built from the resolved owner, so the credential delete
    # targets user_id=12 / slot=5, not the slot index alone.
    assert lock.calls == [("delete_credential", 12, 5)]
    # User stays put -- lifecycle is decoupled from credential presence.
    assert 12 in lock._users


async def test_clear_usercode_resolves_owner_by_tag_when_credential_index_not_slot(
    hass: HomeAssistant,
) -> None:
    """Owner resolution prefers the LCM tag in user.name over credential.slot.

    Regression for #1239 review. After the PR drops the credential_index=slot
    invariant for Matter, a user tagged ``lcm:5:`` may own a PIN whose
    Credential.slot is the Matter-auto-allocated index (e.g. 1), not the
    LCM slot. The owner resolution in async_clear_usercode must find the
    user by the canonical tag in user.name, not by ``credential.slot ==
    code_slot`` alone -- otherwise the loop yields nothing and the clear
    silently no-ops.
    """
    lock = _make_lock(hass, _NativeStubLock, "seam_clear_by_tag")
    lock._users = {
        42: User(
            user_id=42,
            name="lcm:5:Alice",
            # Matter-auto-allocated credential index 1, NOT the LCM slot 5.
            credentials=[credential_from_slot(1, SlotCredential.known("1234"))],
        )
    }
    changed = await lock.async_clear_usercode(5)
    assert changed is True
    # Owner resolved via the lcm:5: tag; delete targets user_id=42
    # with the ref's slot=5 (LCM slot).
    assert lock.calls == [("delete_credential", 42, 5)]
    assert 42 in lock._users


async def test_clear_usercode_legacy_pass_skips_users_tagged_for_other_slots(
    hass: HomeAssistant,
) -> None:
    """Legacy ``credential.slot == code_slot`` fallback ignores tagged owners.

    Regression for #1239 review. A user tagged ``lcm:3:`` whose Matter-
    auto-allocated credential index lands at 7 must NOT be picked up by
    ``async_clear_usercode(7)``'s legacy fallback. Doing so would resolve
    the owner of slot-7 as slot-3's user, and the subsequent delete would
    target the wrong slot.
    """
    lock = _make_lock(hass, _NativeStubLock, "seam_clear_legacy_skips_tagged")
    lock._users = {
        99: User(
            user_id=99,
            name="lcm:3:Alice",
            credentials=[credential_from_slot(7, SlotCredential.known("1234"))],
        )
    }
    # No user owns slot 7 under the canonical-tag lookup, and the legacy
    # fallback must skip the lcm:3:-tagged owner -> clear is a no-op.
    changed = await lock.async_clear_usercode(7)
    assert changed is False
    assert lock.calls == []


async def test_clear_usercode_native_only_clears_the_targeted_credential(
    hass: HomeAssistant,
) -> None:
    """Native clear touches only the targeted credential, never the user.

    Verifies the multi-credential case: even when the lock-side user has
    credentials beyond the one LCM is clearing (e.g. coexisting fingerprint
    or RFID enrolled out-of-band), only the targeted PIN credential is
    deleted. The user record and its other credentials stay put.
    """
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
    assert 4 in lock._users


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
    assert ("set_user", 4, "lcm:4:carol") in lock.calls
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
        "pin": "2468",
        "name": None,
        "source": "sync",
    }


class _CredentialWriteFailsLock(_NativeStubLock):
    """Native stub whose credential write always fails."""

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
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
        ("set_user", 3, "lcm:3:alice"),
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
