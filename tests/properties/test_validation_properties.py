"""Property-based test for credential validation.

The reader-provider design's core guarantee: ``validate_credential`` and the
per-slot active binary sensor render the same predicate, so a reader and the
dashboard can never disagree about whether a credential works. Stated as a
property over generated entry configurations: for any set of slots and any
submitted code, validation succeeds exactly when at least one slot whose
configured PIN equals the code has its active binary sensor on.

The sensor side is read from actual entity states in hass after a full entry
setup -- not from the slot coordinators, which would test the implementation
against itself.
"""

from __future__ import annotations

import asyncio

from hypothesis import given, strategies as st
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from homeassistant import loader
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.lock import LockState
from homeassistant.const import (
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    REASON_CONDITION_NOT_MET,
    REASON_UNKNOWN_CODE,
    REASON_USER_DISABLED,
)
from custom_components.lock_code_manager.domain.validation import validate_credential

from ..common import slot_entity_id

LOCK_ENTITY_ID = "lock.virtual_pbt_validation_virtual"

# A handful of PINs so duplicates across slots actually occur, plus one value
# no slot can hold so the unknown-code outcome is reachable.
PIN_POOL = ("1111", "2222", "3333")
UNKNOWN_CODE = "0000"

CONDITION_STATES = st.sampled_from([STATE_ON, STATE_OFF])


@st.composite
def validation_examples(draw: st.DrawFn) -> tuple[dict[int, dict], dict[str, str], str]:
    """An entry's slot configs, its condition entity states, and a submitted code."""
    slot_count = draw(st.integers(min_value=1, max_value=4))
    slots: dict[int, dict] = {}
    conditions: dict[str, str] = {}
    for slot_num in range(1, slot_count + 1):
        config = {
            CONF_NAME: f"pbt{slot_num}",
            CONF_PIN: draw(st.sampled_from(PIN_POOL)),
            CONF_ENABLED: draw(st.booleans()),
        }
        if draw(st.booleans()):
            entity_id = f"input_boolean.pbt_condition_{slot_num}"
            config[CONF_CONDITION] = entity_id
            conditions[entity_id] = draw(CONDITION_STATES)
        slots[slot_num] = config
    code = draw(st.sampled_from((*PIN_POOL, UNKNOWN_CODE)))
    return slots, conditions, code


async def _assert_validation_matches_sensors(
    hass: HomeAssistant, slots: dict[int, dict], conditions: dict[str, str], code: str
) -> None:
    """Set up a real entry from the drawn slots and check the equivalence."""
    virtual_entry = MockConfigEntry(domain="virtual")
    virtual_entry.add_to_hass(hass)
    lock_entity = er.async_get(hass).async_get_or_create(
        "lock", "virtual", "pbt_validation_virtual", config_entry=virtual_entry
    )
    assert lock_entity.entity_id == LOCK_ENTITY_ID
    hass.states.async_set(LOCK_ENTITY_ID, LockState.LOCKED)
    for entity_id, state in conditions.items():
        hass.states.async_set(entity_id, state)

    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [LOCK_ENTITY_ID], CONF_SLOTS: slots},
        unique_id="pbt_validation",
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    sensor_on = {
        slot_num: hass.states.get(
            slot_entity_id(hass, BINARY_SENSOR_DOMAIN, lcm_entry, slot_num, ATTR_ACTIVE)
        ).state
        == STATE_ON
        for slot_num in slots
    }
    matched = [
        slot_num for slot_num, config in slots.items() if config[CONF_PIN] == code
    ]
    expected = any(sensor_on[slot_num] for slot_num in matched)

    lock = lcm_entry.runtime_data.locks[LOCK_ENTITY_ID]
    result = validate_credential(hass, lcm_entry, lock, code, fire_events=False)

    assert result.valid == expected
    if expected:
        assert result.reason is None
        assert result.user in {
            slots[slot_num][CONF_NAME] for slot_num in matched if sensor_on[slot_num]
        }
    elif matched:
        assert result.reason in (REASON_USER_DISABLED, REASON_CONDITION_NOT_MET)
    else:
        assert result.reason == REASON_UNKNOWN_CODE

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def _run_example(
    loop: asyncio.AbstractEventLoop,
    slots: dict[int, dict],
    conditions: dict[str, str],
    code: str,
) -> None:
    """One example inside its own Home Assistant instance."""
    async with async_test_home_assistant(loop) as hass:
        # The autouse enable_custom_integrations fixture only touches the
        # fixture hass; this instance needs the same custom-component unlock.
        hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
        try:
            await _assert_validation_matches_sensors(hass, slots, conditions, code)
        finally:
            await hass.async_stop(force=True)


@given(example=validation_examples())
def test_validation_agrees_with_active_binary_sensor(
    example: tuple[dict[int, dict], dict[str, str], str],
) -> None:
    """Validation succeeds iff a PIN-matching slot's active sensor reads on.

    Each example builds a fresh Home Assistant on its own loop, the pattern
    the credential machine established: Hypothesis reuses function-scoped
    pytest fixtures across examples, so a fixture hass cannot isolate them.
    """
    slots, conditions, code = example
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_example(loop, slots, conditions, code))
    finally:
        loop.close()
        asyncio.set_event_loop(None)
