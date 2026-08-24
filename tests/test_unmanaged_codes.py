"""Codes on a lock that no config entry accounts for."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.lock_code_manager import async_migrate_entry
from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.domain.unmanaged import unmanaged_issue_id
from custom_components.lock_code_manager.providers import BaseLock
from custom_components.lock_code_manager.repairs import async_create_fix_flow

from .common import BASE_CONFIG, LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID, MockLCMLock

STRANDED_SLOT = 7
STRANDED_PIN = "4242"


@pytest.fixture(name="stranded_before_setup")
def stranded_before_setup_fixture():
    """
    Leave a code on the lock before the entry migrates.

    Ordered ahead of the config entry fixture on purpose: the sweep runs
    once from the migration, so a code programmed after it is a different
    case entirely -- and one this deliberately does not report.
    """
    original = MockLCMLock.__init__

    def seeded(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.codes[STRANDED_SLOT] = STRANDED_PIN

    with patch.object(MockLCMLock, "__init__", seeded):
        yield


@pytest.fixture(name="stranded")
async def stranded_fixture(
    hass: HomeAssistant,
    mock_lock_config_entry,
    stranded_before_setup,
    lock_code_manager_config_entry,
):
    """An entry, migrated to the swept version, whose lock came with a code."""
    entry = lock_code_manager_config_entry
    assert STRANDED_SLOT not in get_entry_config(entry).slots
    assert entry.version == 4
    return entry


@pytest.fixture(name="swept_teardowns")
def swept_teardowns_fixture():
    """Record the locks whose throwaway provider the sweep tears down."""
    torn_down: list[str] = []
    original = BaseLock.unsubscribe_push_updates

    def _record(self) -> None:
        torn_down.append(self.lock.entity_id)
        original(self)

    with patch.object(BaseLock, "unsubscribe_push_updates", _record):
        yield torn_down


def _issue(hass: HomeAssistant, slot: int = STRANDED_SLOT):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, unmanaged_issue_id(LOCK_1_ENTITY_ID, slot)
    )


async def test_the_sweep_tears_down_the_providers_it_builds(
    hass: HomeAssistant, swept_teardowns: list[str], stranded
) -> None:
    """
    Reading a lock can leave transport behind, and nobody else owns it.

    An MQTT provider subscribes to its lock's topics on the way to answering
    a read, so a throwaway that is never torn down goes on firing code slot
    events next to the entry's real provider for the rest of the run.
    """
    assert swept_teardowns == [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]


async def test_a_lock_that_cannot_be_read_is_still_torn_down(
    hass: HomeAssistant,
    mock_lock_config_entry,
    swept_teardowns: list[str],
) -> None:
    """A read that raises is exactly when the leftover subscription is worst."""
    with patch.object(
        MockLCMLock,
        "async_get_usercodes",
        AsyncMock(side_effect=LockDisconnected("lock is asleep")),
    ):
        entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="swept")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert swept_teardowns[:2] == [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_unmanaged_code_raises_its_own_repair(
    hass: HomeAssistant, stranded
) -> None:
    """One issue per code, so each can be decided on its own."""
    issue = _issue(hass)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {
        "lock": LOCK_1_ENTITY_ID,
        "slot": str(STRANDED_SLOT),
    }


async def test_a_managed_code_raises_nothing(hass: HomeAssistant, stranded) -> None:
    """The entry's own codes are not somebody else's problem."""
    for slot in get_entry_config(stranded).slots:
        assert _issue(hass, slot) is None


async def test_a_code_appearing_after_the_sweep_is_not_reported(
    hass: HomeAssistant, stranded
) -> None:
    """
    The sweep settles what was already there, and then stops.

    Reporting every code programmed afterwards would nag the people who
    deliberately keep some of their codes outside this integration, and
    there is no way to tell them from anyone else.
    """
    lock = stranded.runtime_data.locks[LOCK_1_ENTITY_ID]
    await lock.async_internal_set_usercode(9, "9999")
    await lock.coordinator.async_refresh()
    # And a second migration pass finds nothing to do: the entry is already
    # at the swept version.
    assert await async_migrate_entry(hass, stranded)

    assert _issue(hass, 9) is None
    # The one that was there at the sweep is still raised.
    assert _issue(hass) is not None


async def test_clearing_removes_the_code_from_the_lock(
    hass: HomeAssistant, stranded
) -> None:
    """The clear choice actually clears it."""
    lock = stranded.runtime_data.locks[LOCK_1_ENTITY_ID]
    flow = await async_create_fix_flow(
        hass,
        unmanaged_issue_id(LOCK_1_ENTITY_ID, STRANDED_SLOT),
        {"lock_entity_id": LOCK_1_ENTITY_ID, "slot": STRANDED_SLOT},
    )
    flow.hass = hass
    menu = await flow.async_step_init()
    assert menu["menu_options"] == ["clear", "keep"]

    result = await flow.async_step_clear()
    assert result["type"] == "create_entry"
    credential = (await lock.async_get_usercodes()).get(STRANDED_SLOT)
    assert not (credential and credential.pin)


async def test_keeping_leaves_the_code_and_stops_asking(
    hass: HomeAssistant, stranded
) -> None:
    """
    The keep choice is permanent.

    Nothing in Home Assistant ever resets an ignored issue, so a slot
    decided once stays decided.
    """
    lock = stranded.runtime_data.locks[LOCK_1_ENTITY_ID]
    issue_id = unmanaged_issue_id(LOCK_1_ENTITY_ID, STRANDED_SLOT)
    flow = await async_create_fix_flow(
        hass, issue_id, {"lock_entity_id": LOCK_1_ENTITY_ID, "slot": STRANDED_SLOT}
    )
    flow.hass = hass
    await flow.async_step_init()
    result = await flow.async_step_keep()

    assert result["type"] == "abort"
    assert result["reason"] == "unmanaged_code_kept"
    assert (await lock.async_get_usercodes())[STRANDED_SLOT].pin == STRANDED_PIN
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id).dismissed_version

    assert await async_migrate_entry(hass, stranded)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id).dismissed_version


async def test_a_clear_that_fails_leaves_the_repair_standing(
    hass: HomeAssistant, stranded
) -> None:
    """
    A lock that would not take the clear keeps its repair.

    Resolving it would report a clear that did not happen, and the code
    would go on working with nothing left to say so.
    """
    lock = stranded.runtime_data.locks[LOCK_1_ENTITY_ID]
    issue_id = unmanaged_issue_id(LOCK_1_ENTITY_ID, STRANDED_SLOT)
    flow = await async_create_fix_flow(
        hass, issue_id, {"lock_entity_id": LOCK_1_ENTITY_ID, "slot": STRANDED_SLOT}
    )
    flow.hass = hass
    with patch.object(
        lock,
        "async_internal_clear_usercode",
        AsyncMock(side_effect=LockDisconnected("lock is asleep")),
    ):
        result = await flow.async_step_clear()

    assert result["type"] == "abort"
    assert result["reason"] == "unmanaged_code_clear_failed"
    assert (await lock.async_get_usercodes())[STRANDED_SLOT].pin == STRANDED_PIN
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
