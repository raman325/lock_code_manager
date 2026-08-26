"""Test the helpers module."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_PIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    ATTR_LOCK_ENTITY_ID,
    ATTR_USERCODE,
    CONF_CODELESS,
    CONF_LOCKS,
    CONF_MEMBERS,
    CONF_USERS,
    DOMAIN,
    SERVICE_HARD_REFRESH_USERCODES,
    SERVICE_SET_USERCODE,
)
from custom_components.lock_code_manager.domain.locks import (
    get_locks_from_targets,
    get_managed_locks_for_entity,
)
from custom_components.lock_code_manager.domain.queries import get_loaded_config_entry
from custom_components.lock_code_manager.domain.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
)
from custom_components.lock_code_manager.providers.codeless import CodelessLock

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID, MockLCMLock


async def test_get_locks_from_targets_with_entity_ids(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets resolves entity IDs to locks."""
    locks = get_locks_from_targets(hass, {ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID]})

    assert len(locks) == 1
    lock = next(iter(locks))
    assert lock.lock.entity_id == LOCK_1_ENTITY_ID


async def test_get_locks_from_targets_multiple_entity_ids(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets resolves multiple entity IDs."""
    locks = get_locks_from_targets(
        hass, {ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}
    )

    assert len(locks) == 2
    entity_ids = {lock.lock.entity_id for lock in locks}
    assert entity_ids == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}


@pytest.mark.parametrize(
    ("entity_id", "expected_warning"),
    [
        ("switch.not_a_lock", "invalid lock entities"),
        ("lock.unmanaged_lock", "not managed by Lock Code Manager"),
    ],
)
async def test_get_locks_from_targets_warns_for_bad_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
    entity_id: str,
    expected_warning: str,
):
    """Test get_locks_from_targets warns for non-lock and unmanaged entities."""
    locks = get_locks_from_targets(hass, {ATTR_ENTITY_ID: [entity_id]})

    assert len(locks) == 0
    assert expected_warning in caplog.text
    assert entity_id in caplog.text


async def test_get_locks_from_targets_with_area_id(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets resolves area IDs to locks."""
    area_reg = ar.async_get(hass)
    area = area_reg.async_get_or_create("test_area")
    ent_reg = er.async_get(hass)
    ent_reg.async_update_entity(LOCK_1_ENTITY_ID, area_id=area.id)

    locks = get_locks_from_targets(hass, {ATTR_AREA_ID: [area.id]})

    assert len(locks) == 1
    lock = next(iter(locks))
    assert lock.lock.entity_id == LOCK_1_ENTITY_ID


async def test_get_locks_from_targets_with_device_id(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets resolves device IDs to locks."""
    # Get the device for lock.test_1
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(LOCK_1_ENTITY_ID)
    assert entry is not None
    assert entry.device_id is not None

    locks = get_locks_from_targets(hass, {ATTR_DEVICE_ID: [entry.device_id]})

    assert len(locks) == 1
    lock = next(iter(locks))
    assert lock.lock.entity_id == LOCK_1_ENTITY_ID


async def test_get_locks_from_targets_empty(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets with empty target data returns no locks."""
    locks = get_locks_from_targets(hass, {})
    assert len(locks) == 0


async def test_get_locks_from_targets_deduplicates(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets deduplicates when same lock matched by multiple sources."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(LOCK_1_ENTITY_ID)
    assert entry is not None
    assert entry.device_id is not None

    locks = get_locks_from_targets(
        hass,
        {
            ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID],
            ATTR_DEVICE_ID: [entry.device_id],
        },
    )

    assert len(locks) == 1


async def test_get_loaded_config_entry_raises_when_not_loaded(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """get_loaded_config_entry raises ServiceValidationError for an unloaded entry.

    Covers the loaded-vs-not-loaded branch distinct from the "no such
    entry"/"wrong domain" cases already covered via the services tests.
    """
    entry = lock_code_manager_config_entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is not ConfigEntryState.LOADED

    with pytest.raises(ServiceValidationError, match="not loaded"):
        get_loaded_config_entry(hass, entry.entry_id)


async def test_get_loaded_config_entry_requires_an_identifier(
    hass: HomeAssistant,
) -> None:
    """
    Naming no entry is refused, rather than reported as a missing one.

    The service schemas make this unreachable from an action, but the
    function is the shared entry point the websocket uses too, and "no entry
    with ID `None`" would send a caller looking for an entry they never named.
    """
    with pytest.raises(ServiceValidationError, match="Neither"):
        get_loaded_config_entry(hass)


# --- One entity id, two credential stores (issue #1484 review) ---


@pytest.fixture(name="divergent_entries")
async def divergent_entries_fixture(hass: HomeAssistant, mock_lock_config_entry):
    """
    Two loaded entries that resolve one lock entity to different providers.

    Representable because a declaration is per entry: one says the lock keeps
    no codes of its own, so Lock Code Manager holds them, and the other says
    nothing, so the platform provider does. The two credential stores have
    nothing to do with each other, and nothing collapses them.
    """
    lock_entry = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    assert lock_entry is not None
    declaring = MockConfigEntry(
        domain=DOMAIN,
        title="declaring",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_MEMBERS: {lock_entry.id: {CONF_CODELESS: True}},
            CONF_USERS: {"Ada": {CONF_PIN: "1111", CONF_ENABLED: True}},
            CONF_SLOT_ASSIGNMENT: {"ada": 1},
        },
        unique_id="declaring",
    )
    plain = MockConfigEntry(
        domain=DOMAIN,
        title="plain",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {"Bea": {CONF_PIN: "2222", CONF_ENABLED: True}},
            CONF_SLOT_ASSIGNMENT: {"bea": 2},
        },
        unique_id="plain",
    )
    for entry in (declaring, plain):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    yield declaring, plain

    for entry in (declaring, plain):
        await hass.config_entries.async_unload(entry.entry_id)


async def test_each_entrys_own_provider_is_the_one_it_holds(
    hass: HomeAssistant, divergent_entries
):
    """
    Two entries that disagree hold two providers, and both are reachable.

    The old lookups keyed a flat mapping on the entity id, which was
    well-defined only while sharing guaranteed one instance per lock. A
    declaration broke that guarantee, and the flat mapping went on answering
    -- with whichever entry happened to be iterated first.
    """
    declaring, plain = divergent_entries

    found = get_managed_locks_for_entity(hass, LOCK_1_ENTITY_ID)

    # Identity, not class: the point is that each entry gets back the object
    # it is actually driving, and comparing types passes just as well while
    # a caller is handed the other entry's instance.
    assert [id(lock) for lock in found] == [
        id(declaring.runtime_data.locks[LOCK_1_ENTITY_ID]),
        id(plain.runtime_data.locks[LOCK_1_ENTITY_ID]),
    ]
    assert isinstance(declaring.runtime_data.locks[LOCK_1_ENTITY_ID], CodelessLock)
    assert isinstance(plain.runtime_data.locks[LOCK_1_ENTITY_ID], MockLCMLock)


async def test_a_lock_two_entries_disagree_about_is_refused_not_guessed(
    hass: HomeAssistant, divergent_entries
):
    """
    Writing a raw slot refuses rather than picking one of the two stores.

    The action names a lock and nothing else, so with two stores behind that
    name there is no answer it can stand behind. Guessing is not a harmless
    tie-break: the Personal Identification Number lands in a store nobody
    goes on to read, and the codes on screen belong to the other one.
    """
    declaring, plain = divergent_entries
    held_by_lcm = declaring.runtime_data.locks[LOCK_1_ENTITY_ID]
    on_the_device = plain.runtime_data.locks[LOCK_1_ENTITY_ID]
    before = dict(on_the_device.codes)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_USERCODE,
            {
                ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
                ATTR_CODE_SLOT: 7,
                ATTR_USERCODE: "9999",
            },
            blocking=True,
        )

    # Neither store moved, which is the whole of what refusing buys.
    assert on_the_device.codes == before
    assert (await held_by_lcm.async_get_usercodes()).get(7) is None


async def test_a_hard_refresh_reaches_every_provider_for_the_lock(
    hass: HomeAssistant, divergent_entries
):
    """
    An action aimed at a lock reaches both of its credential stores.

    A ``set`` of providers collapsed them, because ``BaseLock`` equality keys
    on the entity id -- so one of the two went on serving whatever it had
    cached, with no sign that it had been skipped.
    """
    declaring, plain = divergent_entries
    held_by_lcm = declaring.runtime_data.locks[LOCK_1_ENTITY_ID]
    on_the_device = plain.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Each provider is left differing from what backs it, so only actually
    # re-reading brings it into line.
    await held_by_lcm._store.async_save({"1": {"code": "1111", "name": "Ada"}})
    held_by_lcm._data = {}
    assert (await held_by_lcm.async_get_usercodes())[1].readable_pin is None
    on_the_device.service_calls["hard_refresh_codes"].clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )

    assert (await held_by_lcm.async_get_usercodes())[1].readable_pin == "1111"
    assert on_the_device.service_calls["hard_refresh_codes"]
