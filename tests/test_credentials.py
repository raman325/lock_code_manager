"""Tests for the cross-platform User/Credential domain model."""

import pytest

from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialRule,
    CredentialState,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    User,
    UserType,
    credential_from_slot,
    slot_credential_of,
    user_from_slot,
)
from custom_components.lock_code_manager.domain.models import SlotCredential


class TestCredentialType:
    """CredentialType wire values must match the live provider vocabulary."""

    def test_pin_wire_value_is_lowercase_pin(self) -> None:
        # The Matter and Z-Wave providers send the literal string "pin".
        assert CredentialType.PIN == "pin"
        assert CredentialType.PIN.value == "pin"

    def test_is_str_enum(self) -> None:
        # StrEnum members compare and serialize as their string value.
        assert isinstance(CredentialType.PIN, str)

    @pytest.mark.parametrize(
        "member",
        ["PIN", "RFID", "FINGERPRINT", "FACE", "PASSWORD", "NFC"],
    )
    def test_reserved_future_members_present(self, member: str) -> None:
        # Reserved so a future multi-credential expansion is purely additive.
        assert member in CredentialType.__members__

    def test_no_unexpected_members(self) -> None:
        assert set(CredentialType.__members__) == {
            "PIN",
            "RFID",
            "FINGERPRINT",
            "FACE",
            "PASSWORD",
            "NFC",
        }


class TestUserType:
    """UserType is modeled minimally; UNRESTRICTED is the default we use today."""

    def test_unrestricted_present_and_is_str(self) -> None:
        assert UserType.UNRESTRICTED == "unrestricted"
        assert isinstance(UserType.UNRESTRICTED, str)

    def test_members(self) -> None:
        assert set(UserType.__members__) == {"UNRESTRICTED"}


class TestCredentialRule:
    """CredentialRule is modeled minimally; SINGLE is the rule we use today."""

    def test_single_present_and_is_str(self) -> None:
        assert CredentialRule.SINGLE == "single"
        assert isinstance(CredentialRule.SINGLE, str)

    def test_members(self) -> None:
        assert set(CredentialRule.__members__) == {"SINGLE"}


class TestCredentialStateAlias:
    """CredentialState is a thin reuse of SlotCredential, not a new dataclass."""

    def test_credential_state_is_slot_credential(self) -> None:
        # Aliasing guarantees there is a single implementation of the three
        # read-states (known / unreadable / empty) shared with the coordinator.
        assert CredentialState is SlotCredential


class TestCredential:
    """A Credential pairs a type and slot with a reused SlotCredential state."""

    def test_known_pin_is_readable_and_present(self) -> None:
        cred = Credential(
            type=CredentialType.PIN, slot=3, state=SlotCredential.known("1234")
        )
        assert cred.is_present
        assert cred.is_readable
        assert not cred.is_empty
        assert cred.readable_pin == "1234"
        assert cred.matches("1234")
        assert not cred.matches("0000")

    def test_unreadable_is_present_not_readable(self) -> None:
        cred = Credential(
            type=CredentialType.PIN, slot=3, state=SlotCredential.unreadable()
        )
        assert cred.is_present
        assert not cred.is_readable
        assert not cred.is_empty
        assert cred.readable_pin is None
        assert not cred.matches("1234")

    def test_empty_is_neither_present_nor_readable(self) -> None:
        cred = Credential(type=CredentialType.PIN, slot=3, state=SlotCredential.empty())
        assert cred.is_empty
        assert not cred.is_present
        assert not cred.is_readable
        assert cred.readable_pin is None

    def test_is_frozen(self) -> None:
        cred = Credential(type=CredentialType.PIN, slot=3, state=SlotCredential.empty())
        with pytest.raises((AttributeError, TypeError)):
            cred.slot = 4  # type: ignore[misc]


class TestCredentialRef:
    """CredentialRef addresses a credential as (user_id, type, slot)."""

    def test_field_order_and_values(self) -> None:
        ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
        assert ref.user_id == 3
        assert ref.type == CredentialType.PIN
        assert ref.slot == 3
        # NamedTuple positional order is part of the contract.
        assert tuple(ref) == (3, CredentialType.PIN, 3)

    def test_is_hashable_and_value_equal(self) -> None:
        # Usable as a dict key / set member for addressing.
        a = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        b = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        assert a == b
        assert len({a, b}) == 1


class TestUser:
    """A User owns a list of credentials; defaults model today's single user."""

    def test_defaults_are_minimal(self) -> None:
        user = User(user_id=3)
        assert user.user_id == 3
        assert user.name is None
        assert user.user_type is UserType.UNRESTRICTED
        assert user.active is True
        assert user.credential_rule is CredentialRule.SINGLE
        assert user.credentials == []

    def test_each_user_gets_its_own_credentials_list(self) -> None:
        # A mutable default must not be shared across instances.
        a = User(user_id=1)
        b = User(user_id=2)
        assert a.credentials is not b.credentials

    def test_holds_credentials(self) -> None:
        cred = Credential(
            type=CredentialType.PIN, slot=3, state=SlotCredential.known("1234")
        )
        user = User(user_id=3, name="alice", credentials=[cred])
        assert user.credentials == [cred]
        assert user.name == "alice"

    def test_pin_credentials_filters_by_type(self) -> None:
        pin = Credential(
            type=CredentialType.PIN, slot=3, state=SlotCredential.known("1234")
        )
        rfid = Credential(
            type=CredentialType.RFID, slot=3, state=SlotCredential.unreadable()
        )
        user = User(user_id=3, credentials=[pin, rfid])
        assert user.pin_credentials == [pin]

    def test_credential_for_returns_first_match_or_none(self) -> None:
        empty_pin = Credential(
            type=CredentialType.PIN, slot=3, state=SlotCredential.empty()
        )
        user = User(user_id=3, credentials=[empty_pin])
        assert user.credential_for(CredentialType.PIN) is empty_pin
        assert user.credential_for(CredentialType.RFID) is None


class TestLockCapabilities:
    """LockCapabilities describes user management and per-type slot limits."""

    def test_per_type_capability_fields(self) -> None:
        cap = CredentialTypeCapability(
            num_slots=30, min_length=4, max_length=8, supports_learn=False
        )
        assert cap.num_slots == 30
        assert cap.min_length == 4
        assert cap.max_length == 8
        assert cap.supports_learn is False

    def test_capabilities_expose_per_type_lookup(self) -> None:
        pin_cap = CredentialTypeCapability(
            num_slots=30, min_length=4, max_length=8, supports_learn=False
        )
        caps = LockCapabilities(
            supports_user_management=True,
            max_users=30,
            credential_types={CredentialType.PIN: pin_cap},
        )
        assert caps.supports_user_management is True
        assert caps.max_users == 30
        assert caps.capability_for(CredentialType.PIN) is pin_cap
        assert caps.capability_for(CredentialType.RFID) is None
        assert caps.supports(CredentialType.PIN)
        assert not caps.supports(CredentialType.RFID)

    def test_capabilities_are_frozen(self) -> None:
        caps = LockCapabilities(
            supports_user_management=False, max_users=0, credential_types={}
        )
        with pytest.raises((AttributeError, TypeError)):
            caps.max_users = 5  # type: ignore[misc]


class TestProjectionHelpers:
    """Pure 1:1:1 projection between a managed slot and the User/Credential model."""

    @pytest.mark.parametrize(
        "state",
        [
            SlotCredential.known("1234"),
            SlotCredential.unreadable(),
            SlotCredential.empty(),
        ],
    )
    def test_credential_from_slot_shares_index_and_state(
        self, state: SlotCredential
    ) -> None:
        cred = credential_from_slot(5, state)
        assert cred.type is CredentialType.PIN
        assert cred.slot == 5
        # The SlotCredential is reused verbatim as the credential state.
        assert cred.state is state

    @pytest.mark.parametrize(
        "state",
        [
            SlotCredential.known("1234"),
            SlotCredential.unreadable(),
            SlotCredential.empty(),
        ],
    )
    def test_slot_credential_of_round_trips(self, state: SlotCredential) -> None:
        cred = credential_from_slot(5, state)
        # Projecting the PIN credential back to a SlotCredential is identity.
        assert slot_credential_of(cred) is state

    def test_slot_credential_of_rejects_non_pin(self) -> None:
        rfid = Credential(
            type=CredentialType.RFID, slot=5, state=SlotCredential.unreadable()
        )
        with pytest.raises(ValueError):
            slot_credential_of(rfid)

    def test_user_from_slot_is_single_pin_user_sharing_index(self) -> None:
        state = SlotCredential.known("1234")
        user = user_from_slot(5, state)
        assert user.user_id == 5
        assert user.user_type is UserType.UNRESTRICTED
        assert user.credential_rule is CredentialRule.SINGLE
        # Active mirrors presence: an empty slot is an inactive user today.
        assert user.active is True
        assert len(user.credentials) == 1
        cred = user.credentials[0]
        assert cred.type is CredentialType.PIN
        assert cred.slot == 5
        assert cred.state is state

    def test_user_from_slot_empty_is_inactive(self) -> None:
        user = user_from_slot(5, SlotCredential.empty())
        assert user.active is False
        assert user.credentials[0].is_empty

    def test_user_from_slot_accepts_optional_name(self) -> None:
        user = user_from_slot(5, SlotCredential.known("1234"), name="alice")
        assert user.name == "alice"
