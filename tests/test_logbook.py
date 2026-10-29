"""
A credential use is written to the logbook of what it was used against.

Home Assistant's logbook shows an entity only its own integration's described
events, and Lock Code Manager owns none of the locks it manages. So the entry
has to be a manual logbook entry, the one kind of event the logbook shows for
any entity whose ``entity_id`` it names. These tests query the logbook the way
a lock's more-info dialog does -- by the lock's entity ID -- so they fail if
the entry lands anywhere a person looking at the lock would not see it.
"""

from datetime import timedelta
import itertools
from typing import Any

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.logbook import DOMAIN as LOGBOOK_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_SOURCE,
    ATTR_TARGET,
    DOMAIN,
    SERVICE_USE_CREDENTIAL,
)
from custom_components.lock_code_manager.model.credentials import CredentialType
from custom_components.lock_code_manager.model.events import (
    CredentialOperation,
    credential_use_message,
)
from custom_components.lock_code_manager.providers import BaseLock

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID

EXTERNAL_KEYPAD = "sensor.side_gate_keypad"
EXTERNAL_TARGET = "cover.side_gate"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """
    Claim the recorder database before Home Assistant starts.

    Overrides the project-wide autouse fixture of the same name, which pulls
    in `hass` before `recorder_db_url` may run.
    """
    yield


@pytest.fixture(name="logbook")
async def logbook_fixture(recorder_mock, hass: HomeAssistant) -> None:
    """
    Run the real logbook over the real recorder.

    The logbook depends on the frontend only to register its panel, and the
    frontend's built files are not installed for tests, so the frontend is
    marked loaded rather than set up.
    """
    hass.config.components.add("frontend")
    assert await async_setup_component(hass, LOGBOOK_DOMAIN, {})
    await hass.async_block_till_done()


async def _logbook_for(hass_ws_client, hass: HomeAssistant, entity_id: str) -> list:
    """Return the logbook entries for one entity, as its more-info dialog asks."""
    await async_wait_recording_done(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "logbook/get_events",
            "start_time": (dt_util.utcnow() - timedelta(hours=1)).isoformat(),
            "entity_ids": [entity_id],
        }
    )
    response = await client.receive_json()
    assert response["success"], response
    return response["result"]


def _uses(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the entries Lock Code Manager wrote, dropping the lock's own states."""
    return [entry for entry in entries if entry.get("domain") == DOMAIN]


async def test_a_lock_observed_use_is_in_the_locks_logbook(
    hass: HomeAssistant,
    hass_ws_client,
    logbook,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """One use the lock observed is one entry on that lock, naming the user."""
    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.async_fire_code_slot_event(2, False, credential_type=CredentialType.PIN)
    await hass.async_block_till_done()

    uses = _uses(await _logbook_for(hass_ws_client, hass, LOCK_1_ENTITY_ID))
    assert len(uses) == 1
    assert uses[0]["entity_id"] == LOCK_1_ENTITY_ID
    assert uses[0]["name"] == "test2"
    assert uses[0]["message"] == "used a PIN to unlock"

    assert _uses(await _logbook_for(hass_ws_client, hass, LOCK_2_ENTITY_ID)) == []


async def test_a_reported_use_is_in_its_targets_logbook(
    hass: HomeAssistant,
    hass_ws_client,
    logbook,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A use reported through the action lands on its target, whatever that is.

    The target here is a cover no integration even provides: the entry names
    it, so the logbook files it there.
    """
    await hass.services.async_call(
        DOMAIN,
        SERVICE_USE_CREDENTIAL,
        {
            "config_entry_id": lock_code_manager_config_entry.entry_id,
            ATTR_CODE: "1234",
            ATTR_SOURCE: EXTERNAL_KEYPAD,
            ATTR_TARGET: EXTERNAL_TARGET,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    uses = _uses(await _logbook_for(hass_ws_client, hass, EXTERNAL_TARGET))
    assert len(uses) == 1
    assert uses[0]["entity_id"] == EXTERNAL_TARGET
    assert uses[0]["name"] == "test1"
    assert uses[0]["message"] == "used a PIN"

    # The source can hold the code that was typed; it is never where the
    # entry is filed.
    assert _uses(await _logbook_for(hass_ws_client, hass, EXTERNAL_KEYPAD)) == []


@pytest.mark.parametrize(
    ("credential_type", "operation"),
    list(itertools.product(CredentialType, CredentialOperation)),
)
def test_every_use_reads_as_a_sentence_naming_its_kind(
    credential_type: CredentialType, operation: CredentialOperation
):
    """
    Every kind and operation has a message, and only a known operation is named.

    The logbook prints the user's name in front of the message, so it has to
    read on from a name. Exhaustive rather than sampled: the space is small
    and a new member of either enum must not ship without words.
    """
    message = credential_use_message(credential_type, operation)

    assert message.startswith("used ")
    if operation is CredentialOperation.UNKNOWN:
        assert " to " not in message
    else:
        assert message.endswith(f" to {operation}")


@pytest.mark.parametrize("operation", list(CredentialOperation))
def test_no_two_kinds_read_the_same(operation: CredentialOperation):
    """A card read and a fingerprint must not both read as the same use."""
    messages = {credential_use_message(kind, operation) for kind in CredentialType}
    assert len(messages) == len(CredentialType)
