"""
Configured PINs never reach Lock Code Manager's own log records (#917).

Each test runs an entry at DEBUG through what writes and reports PINs --
initial sync, a PIN change, the credential and user services, a reported
use -- and searches every record Lock Code Manager's loggers made for the
digits. The PINs are eight digits so that a match cannot be an id or a
timestamp that happens to contain them.
"""

import logging

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_SOURCE,
    ATTR_TARGET,
    DOMAIN,
    SERVICE_DISABLE_USER,
    SERVICE_ENABLE_USER,
    SERVICE_SET_CREDENTIAL,
    SERVICE_USE_CREDENTIAL,
)
from custom_components.lock_code_manager.model.queries import get_entry_config

from .common import (
    LOCK_1_ENTITY_ID,
    async_advance_sync_ticks,
    async_set_slot_pin,
    async_tick_until,
    current_entry,
    in_sync_entity_id,
    lcm_records_holding,
    mock_lock,
)

_LCM_LOGGER = "custom_components.lock_code_manager"
FIRST_PIN = "73915286"
SECOND_PIN = "60482917"
SET_PIN = "58203641"


async def test_no_configured_pin_reaches_a_log_record(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for #917 (PRs #923, #1097): sync, services and uses log no PIN."""
    caplog.set_level(logging.DEBUG, logger=_LCM_LOGGER)
    entry = current_entry("Quiet Logs", {1: FIRST_PIN})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    in_sync = in_sync_entity_id(hass, entry, 1, LOCK_1_ENTITY_ID)
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=10,
        what="slot 1 in sync",
    )
    name = get_entry_config(entry).name_for(1)

    await async_set_slot_pin(hass, entry, 1, SECOND_PIN)
    await async_tick_until(
        hass,
        freezer,
        lambda: mock_lock(entry).codes.get(1) == SECOND_PIN,
        max_ticks=10,
        what="the changed PIN on the lock",
    )
    for service, data in (
        (
            SERVICE_SET_CREDENTIAL,
            {"name": name, "credential_type": "pin", "value": SET_PIN},
        ),
        (SERVICE_DISABLE_USER, {"name": name}),
        (SERVICE_ENABLE_USER, {"name": name}),
        (
            SERVICE_USE_CREDENTIAL,
            {
                ATTR_CODE: SET_PIN,
                ATTR_SOURCE: "sensor.keypad",
                ATTR_TARGET: "cover.gate",
            },
        ),
    ):
        await hass.services.async_call(
            DOMAIN,
            service,
            {"config_entry_id": entry.entry_id, **data},
            blocking=True,
        )
        await async_advance_sync_ticks(hass, freezer, 5)
        if service == SERVICE_SET_CREDENTIAL:
            await async_tick_until(
                hass,
                freezer,
                lambda: mock_lock(entry).codes.get(1) == SET_PIN,
                max_ticks=10,
                what="the credential service's PIN on the lock",
            )

    if not any(
        r.name.startswith(_LCM_LOGGER) and r.levelno == logging.DEBUG
        for r in caplog.records
    ):
        pytest.fail("setup: nothing was logged at DEBUG")
    assert lcm_records_holding(caplog, (FIRST_PIN, SECOND_PIN, SET_PIN)) == []
    assert await hass.config_entries.async_unload(entry.entry_id)
