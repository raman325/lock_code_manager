"""Tests for the cross-platform User/Credential domain model."""

import pytest

from custom_components.lock_code_manager.domain.credentials import CredentialType


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


from custom_components.lock_code_manager.domain.credentials import (  # noqa: E402
    CredentialRule,
    UserType,
)


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


from custom_components.lock_code_manager.domain.credentials import (  # noqa: E402
    Credential,
    CredentialState,
)
from custom_components.lock_code_manager.domain.models import (  # noqa: E402
    SlotCredential,
)


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


from custom_components.lock_code_manager.domain.credentials import (  # noqa: E402
    CredentialRef,
)


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
