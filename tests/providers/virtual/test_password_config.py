"""A password in the configuration, end to end on a Virtual lock."""

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.ha.entry import available_credential_types
from custom_components.lock_code_manager.model.credentials import CredentialType


async def test_an_entry_with_a_password_lock_offers_passwords(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The entry's types are the union of what its locks take."""
    assert available_credential_types(lcm_config_entry) == {
        CredentialType.PIN,
        CredentialType.PASSWORD,
    }
