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
