"""Codes on a lock that no config entry accounts for."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.lock_code_manager import async_migrate_entry
from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.lock_runtime import (
    async_give_up,
    async_write_once,
    lock_registry,
)
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.domain.unmanaged import unmanaged_issue_id
from custom_components.lock_code_manager.providers import BaseLock
from custom_components.lock_code_manager.repairs import async_create_fix_flow

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    MockLCMLock,
    async_setup_lcm_entry,
    mock_lock,
    user_subentries,
)

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
    assert entry.version == 5
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


async def test_a_disabled_entrys_code_on_a_shared_lock_is_not_reported(
    hass: HomeAssistant, mock_lock_config_entry, stranded_before_setup
) -> None:
    """
    A code a disabled entry's user holds is that entry's, not a stranger's.

    Reported as unmanaged, the repair would offer to delete it, and enabling
    the entry again would find its user's code gone.
    """
    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [LOCK_1_ENTITY_ID]},
        subentries_data=user_subentries(
            {
                STRANDED_SLOT: {
                    CONF_NAME: "held",
                    CONF_PIN: STRANDED_PIN,
                    CONF_ENABLED: True,
                }
            }
        ),
        unique_id="holder",
        disabled_by=ConfigEntryDisabler.USER,
    ).add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="swept")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 5

    assert _issue(hass) is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


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
    lock.codes[9] = "9999"
    await lock_registry(hass).worker(LOCK_1_ENTITY_ID).read()
    await hass.async_block_till_done()
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
    # The lock acknowledges the clear and keeps the code: every read-back
    # still finds it, so the slot is held after its tries.
    lock.ignore("clear", STRANDED_SLOT)
    result = await flow.async_step_clear()
    lock.clear_faults()

    assert result["type"] == "abort"
    assert result["reason"] == "unmanaged_code_clear_failed"
    assert result["description_placeholders"]["link_health"] == ""
    assert (await lock.async_get_usercodes())[STRANDED_SLOT].pin == STRANDED_PIN
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_a_failed_clear_quotes_the_lock_link_health(
    hass: HomeAssistant, stranded
) -> None:
    """
    A failed clear carries the provider's transport counters.

    The failure message alone cannot distinguish a lock that rejected the
    clear from a link that dropped the reply; whatever the provider can
    measure about its transport goes in so the reader can settle it.
    """
    lock = stranded.runtime_data.locks[LOCK_1_ENTITY_ID]
    issue_id = unmanaged_issue_id(LOCK_1_ENTITY_ID, STRANDED_SLOT)
    flow = await async_create_fix_flow(
        hass, issue_id, {"lock_entity_id": LOCK_1_ENTITY_ID, "slot": STRANDED_SLOT}
    )
    flow.hass = hass
    lock.ignore("clear", STRANDED_SLOT)
    with patch.object(
        lock,
        "describe_link_health",
        return_value="39 read requests went unanswered",
    ):
        result = await flow.async_step_clear()
    lock.clear_faults()

    assert result["type"] == "abort"
    assert result["reason"] == "unmanaged_code_clear_failed"
    assert (
        result["description_placeholders"]["link_health"]
        == "\n\n39 read requests went unanswered"
    )


async def test_a_refused_clear_is_reported_as_a_clear(
    hass: HomeAssistant, stranded
) -> None:
    """The repair's error says the lock would not clear the code, not set it."""
    lock = stranded.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.refuse("clear", STRANDED_SLOT)
    flow = await async_create_fix_flow(
        hass,
        unmanaged_issue_id(LOCK_1_ENTITY_ID, STRANDED_SLOT),
        {"lock_entity_id": LOCK_1_ENTITY_ID, "slot": STRANDED_SLOT},
    )
    flow.hass = hass

    result = await flow.async_step_clear()

    lock.clear_faults()
    assert result["reason"] == "unmanaged_code_clear_failed"
    error = result["description_placeholders"]["error"]
    assert "set code" not in error
    assert "clear" in error


async def test_an_unmanaged_clear_after_a_release_is_a_plain_clear(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A slot given up long ago, now holding someone else's code.

    ``releasing`` keeps the slot after its release is done, so the card's
    clear of the unmanaged code goes out as a release: a native-user
    provider (Matter) deletes only an LCM-tagged user and leaves the code,
    and on a masked lock the clear is then trusted (#1437) -- reported done.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="rt")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)

    async_give_up(hass, entry, LOCK_1_ENTITY_ID, 2)
    await hass.async_block_till_done()
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert worker is not None and 2 not in worker.slots
    assert 2 not in lock.codes

    # Someone programs a code at the keypad; the user clears it from the card.
    lock.codes[2] = "9999"
    releases: list[int] = []

    async def _release(self, slot: int) -> None:
        releases.append(slot)  # no LCM-tagged user here: nothing deleted

    with patch.object(type(lock), "async_release_managed_slot", _release):
        done = await async_write_once(hass, LOCK_1_ENTITY_ID, 2, None)
        await hass.async_block_till_done()

    assert releases == []
    assert 2 not in lock.codes
    assert done is True
    assert 2 not in worker.port.releasing


async def test_clearing_an_unmanaged_code_on_a_lock_no_entry_holds(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The lock left every entry after the sweep: the repair says so, at once.

    The old flow refused with "not managed". Now the clear starts a worker
    for a provider nobody sets up: every write is unsent, the flow waits a
    minute and then reports a failed clear "after two tries" that never
    reached the lock -- and a worker is left running for the lock.
    """
    await async_setup_lcm_entry(hass)  # holds lock 1 only
    flow = await async_create_fix_flow(
        hass,
        unmanaged_issue_id(LOCK_2_ENTITY_ID, 2),
        {"lock_entity_id": LOCK_2_ENTITY_ID, "slot": 2},
    )
    flow.hass = hass
    started = time.monotonic()
    result = await flow.async_step_clear()
    await hass.async_block_till_done()

    assert time.monotonic() - started < 10
    assert result["type"] == "abort"
    assert "two tries" not in result["description_placeholders"].get("error", "")
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None
