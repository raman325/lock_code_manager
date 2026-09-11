"""Behavioral tests for the shipped Lock Code Manager blueprints.

Each blueprint is loaded via ``patch_blueprint`` so the real YAML on disk is
exercised end-to-end: trigger fires from a real LCM event entity, the
automation runs, and we assert on the side effects via mocked services.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import pathlib
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache_with_extra_data,
)

from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.components.template import config as template_config
from homeassistant.const import ATTR_FRIENDLY_NAME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, ServiceCall, State, callback
from homeassistant.helpers import entity_registry as er, template
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_CODE_SLOT,
    ATTR_SLOT_FIELD,
    ATTR_SOURCE,
    ATTR_TARGET,
    DOMAIN,
    SERVICE_USE_CREDENTIAL,
)
from custom_components.lock_code_manager.providers import BaseLock

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_EVENT_ENTITY,
    SLOT_2_EVENT_ENTITY,
)

BLUEPRINTS_ROOT = pathlib.Path(__file__).resolve().parent.parent / "blueprints"
BLUEPRINT_FOLDER = BLUEPRINTS_ROOT / "automation" / "lock_code_manager"

NOTIFIER_PATH = "slot_usage_notifier.yaml"
LIMITER_PATH = "slot_usage_limiter.yaml"

# Template blueprints live under a different root, so this one carries the
# folder its path is relative to.
CALENDAR_PATH = "lock_code_manager/calendar_condition.yaml"


@contextlib.contextmanager
def patch_blueprint(
    blueprint_path: str,
    data_path: pathlib.Path,
    schema: Any = automation.config.AUTOMATION_BLUEPRINT_SCHEMA,
) -> Iterator[None]:
    """Intercept blueprint loading so HA reads our repo YAML directly.

    Mirrors the helper used in homeassistant.tests.components.automation.test_blueprint.
    ``schema`` is the blueprint schema of the domain being loaded: the
    template domain validates its blueprints against its own.
    """
    orig_load = models.DomainBlueprints._load_blueprint

    @callback
    def mock_load(self, path):
        if path != blueprint_path:
            return orig_load(self, path)
        return models.Blueprint(
            yaml_util.load_yaml(data_path),
            expected_domain=self.domain,
            path=path,
            schema=schema,
        )

    with patch(
        "homeassistant.components.blueprint.models.DomainBlueprints._load_blueprint",
        mock_load,
    ):
        yield


async def _setup_blueprint_automations(
    hass: HomeAssistant, *automations: tuple[str, dict]
) -> None:
    """
    Instantiate several blueprint-based automations in one component setup.

    ``async_setup_component`` is a no-op once the automation domain is up, so
    every automation a test needs has to arrive in the same call. Setting a
    second one up on its own leaves it silently nonexistent, and an assertion
    that it never fired then passes for the wrong reason.
    """
    with contextlib.ExitStack() as stack:
        for blueprint_path, _ in automations:
            stack.enter_context(
                patch_blueprint(blueprint_path, BLUEPRINT_FOLDER / blueprint_path)
            )
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": [
                    {"use_blueprint": {"path": blueprint_path, "input": inputs}}
                    for blueprint_path, inputs in automations
                ]
            },
        )
    await hass.async_block_till_done()


async def _setup_blueprint_automation(
    hass: HomeAssistant, blueprint_path: str, inputs: dict
) -> None:
    """Instantiate a blueprint-based automation with the given inputs."""
    await _setup_blueprint_automations(hass, (blueprint_path, inputs))


def _fire_pin_used(
    config_entry,
    lock_entity_id: str,
    slot: int,
    *,
    to_locked: bool = False,
) -> None:
    """Fire a PIN-used event from the named lock on the named slot.

    ``to_locked`` is the direction the lock moved: False for a code that
    unlocked the door, True for one that locked it.
    """
    lock: BaseLock = config_entry.runtime_data.locks[lock_entity_id]
    lock.async_fire_code_slot_event(slot, to_locked)


# A keypad and a gate Lock Code Manager manages neither of, which is the
# point: a use entered on one of these is one no lock in the entry observed.
EXTERNAL_KEYPAD = "sensor.side_gate_keypad"
EXTERNAL_TARGET = "cover.side_gate"


async def _use_credential(
    hass: HomeAssistant, config_entry, target: str = EXTERNAL_TARGET
) -> None:
    """Report a credential use whose source and target are different entities."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_USE_CREDENTIAL,
        {
            "config_entry_id": config_entry.entry_id,
            ATTR_CODE: "1234",
            ATTR_SOURCE: EXTERNAL_KEYPAD,
            ATTR_TARGET: target,
        },
        blocking=True,
    )


# --------------------------------------------------------------------------- #
# Slot Usage Notifier
# --------------------------------------------------------------------------- #


async def test_notifier_fires_for_single_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Without a lock filter, every PIN use on the configured entity triggers."""
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [
                {
                    "service": "test.captured",
                    "data": {
                        "slot_num": "{{ slot_num }}",
                        "slot_name": "{{ slot_name }}",
                        "lock_name": "{{ lock_name }}",
                    },
                }
            ],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert len(captured) == 1
    call: ServiceCall = captured[0]
    # `{{ slot_num }}` renders as int 1 since HA preserves template result types.
    assert int(call.data["slot_num"]) == 1
    assert call.data["slot_name"] == "test1"
    # MockLockEntity registers with friendly name = "test_1"
    assert call.data["lock_name"] == "test_1"


async def test_notifier_names_the_user_for_a_reported_use(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A use reported through ``use_credential`` still names the user.

    Such a use records the unified payload, which names the user in `name`
    and has no `code_slot_name` at all, so reading only the latter left
    every keypad notification saying "Unknown slot".
    """
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [
                {
                    "service": "test.captured",
                    "data": {
                        "slot_num": "{{ slot_num }}",
                        "slot_name": "{{ slot_name }}",
                        "lock_name": "{{ lock_name }}",
                    },
                }
            ],
        },
    )

    # Targets a lock in the entry, which is what makes the slot entity record
    # it; the source is the keypad, so this is not a use the lock observed.
    await _use_credential(hass, lock_code_manager_config_entry, target=LOCK_1_ENTITY_ID)
    await hass.async_block_till_done()

    # Should the payload ever gain `code_slot_name`, this test stops
    # exercising the fallback and should be rewritten rather than pass on.
    recorded = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert "code_slot_name" not in recorded.attributes

    assert len(captured) == 1
    call: ServiceCall = captured[0]
    assert call.data["slot_name"] == "test1"
    assert int(call.data["slot_num"]) == 1
    assert call.data["lock_name"] == "test_1"


async def test_notifier_fires_for_multiple_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """PR #1159: configuring multiple event entities fires for any of them."""
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY, SLOT_2_EVENT_ENTITY],
            "notify_actions": [
                {"service": "test.captured", "data": {"slot": "{{ slot_num }}"}}
            ],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 2)
    await hass.async_block_till_done()

    assert [int(call.data["slot"]) for call in captured] == [1, 2]


async def test_notifier_lock_filter_allows_match(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """PR #1152: action runs when the firing lock is in the configured set."""
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "locks": [LOCK_1_ENTITY_ID],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert len(captured) == 1


async def test_notifier_lock_filter_blocks_non_match(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """PR #1152: action is gated out when the firing lock isn't in the set."""
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "locks": [LOCK_1_ENTITY_ID],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_2_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert captured == []


async def test_notifier_fires_on_first_pin_use(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """The very first PIN use on a fresh slot fires the notifier.

    Case: `'unknown' -> <timestamp>`. A freshly-registered LCM event
    entity sits at `unknown` until its first event. The condition
    only blocks `from_state is None` and
    `from_state.state == 'unavailable'`, so this transition is
    allowed.
    """
    captured = async_mock_service(hass, "test", "captured")

    assert hass.states.get(SLOT_1_EVENT_ENTITY).state == "unknown"

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert len(captured) == 1


async def test_notifier_fires_on_subsequent_pin_use(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Subsequent PIN uses fire the notifier.

    Case: `<old_timestamp> -> <new_timestamp>`. The second fire
    transitions between two valid timestamps; the condition passes
    on both legs.
    """
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert len(captured) == 1
    first_state = hass.states.get(SLOT_1_EVENT_ENTITY).state

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert len(captured) == 2
    # Sanity check: the second fire genuinely came from a timestamp
    # state, not from `unknown` (i.e. case 8, not a repeat of case 4).
    assert first_state != "unknown"


async def test_notifier_skips_unavailable_to_timestamp(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Recovery from `unavailable` does not fire the notifier.

    When LCM or its underlying lock integration reloads while HA is
    running, the event entity can briefly drop to `unavailable` before
    its supporting lock comes back online. Without the condition that
    rejects `from_state.state == 'unavailable'`, the transition back
    to a real timestamp would spuriously fire the trigger.
    """
    captured = async_mock_service(hass, "test", "captured")

    # Force the entity into `unavailable` so the next state write
    # produces an `unavailable -> timestamp` transition.
    hass.states.async_set(SLOT_1_EVENT_ENTITY, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(SLOT_1_EVENT_ENTITY).state == "unavailable"

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert captured == []
    assert hass.states.get(SLOT_1_EVENT_ENTITY).state != "unavailable"


async def test_notifier_skips_entity_appearance_with_restored_state(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Entity appearing with a restored value does not fire the notifier.

    This is the LCM-reload / fast-restart scenario: HA's recorder
    restores the slot's last-fired timestamp before LCM finishes
    setting up, and the automation listener happens to be registered
    before the entity reappears. The resulting `state_changed` event
    has `old_state = None` (Python None, not the string 'unknown') —
    which `not_from: [unknown, unavailable]` could never block, and
    which the from_state condition explicitly catches.

    We construct the scenario by priming the restore cache before LCM,
    then registering the automation before LCM's config entry is set
    up so the listener is in place when the entity appears.
    """
    captured = async_mock_service(hass, "test", "captured")

    restored_ts = "2026-01-01T12:00:00.000+00:00"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(
                    SLOT_1_EVENT_ENTITY,
                    restored_ts,
                    {
                        "event_type": LOCK_1_ENTITY_ID,
                        "code_slot": 1,
                        "code_slot_name": "test1",
                        ATTR_FRIENDLY_NAME: "Code slot 1",
                    },
                ),
                {
                    "last_event_type": LOCK_1_ENTITY_ID,
                    "last_event_attributes": {
                        "code_slot": 1,
                        "code_slot_name": "test1",
                    },
                },
            )
        ],
    )

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(SLOT_1_EVENT_ENTITY).state == restored_ts
    assert captured == []


async def test_notifier_skips_fresh_entity_appearance(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Entity appearing fresh (no restored data) does not fire the notifier.

    Case: `None -> 'unknown'`. The state-trigger's `not_to: unknown`
    short-circuits before the condition even runs, but verifying this
    transition is silent is important — if a future refactor drops
    `not_to`, the `from_state is None` condition would still need to
    catch it (which it does).
    """
    captured = async_mock_service(hass, "test", "captured")

    # Register the automation BEFORE LCM so the listener is active
    # when the entity first appears. The `event_entity` selector
    # accepts an entity_id even if the entity doesn't exist yet —
    # blueprint setup wires up a state-change listener regardless.
    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(SLOT_1_EVENT_ENTITY).state == "unknown"
    assert captured == []


async def test_notifier_skips_transition_to_unavailable(
    hass: HomeAssistant,
    caplog,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Going offline does not fire the notifier.

    Case: `<timestamp> -> 'unavailable'`. Blocked at the trigger
    level by `not_to: unavailable`. Without that filter the trigger
    would fire and the action's variable rendering would crash on
    the now-empty attributes — `captured == []` alone would still
    hold (silent failure), so we also assert no rendering errors
    were logged.
    """
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert len(captured) == 1
    captured.clear()
    caplog.clear()

    # Force the entity to `unavailable`, simulating the lock going
    # offline after a real PIN use.
    hass.states.async_set(SLOT_1_EVENT_ENTITY, "unavailable")
    await hass.async_block_till_done()

    assert captured == []
    assert "Error rendering variables" not in caplog.text


async def test_notifier_skips_transition_to_unknown(
    hass: HomeAssistant,
    caplog,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Losing state does not fire the notifier.

    Case: `<timestamp> -> 'unknown'`. Symmetric to the unavailable
    case — blocked at the trigger level by `not_to: unknown`.
    """
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert len(captured) == 1
    captured.clear()
    caplog.clear()

    hass.states.async_set(SLOT_1_EVENT_ENTITY, "unknown")
    await hass.async_block_till_done()

    assert captured == []
    assert "Error rendering variables" not in caplog.text


async def test_notifier_skips_entity_removal(
    hass: HomeAssistant,
    caplog,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Entity removal does not fire the notifier (and does not crash).

    Case: `<timestamp> -> None`. The `not_to` filter does NOT block
    this (Python None is not the string 'unknown' or 'unavailable'),
    so the `trigger.to_state is not none` condition is what catches
    it. Without that guard the trigger would fire and the action
    would crash trying to read `trigger.to_state.attributes` — the
    service still wouldn't be called (so `captured == []` alone
    can't detect a regression), but the log would carry a template
    error. We assert both: no service call AND no template error.
    """
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured"}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert len(captured) == 1
    captured.clear()
    caplog.clear()

    hass.states.async_remove(SLOT_1_EVENT_ENTITY)
    await hass.async_block_till_done()

    assert captured == []
    assert hass.states.get(SLOT_1_EVENT_ENTITY) is None
    assert "Error rendering variables" not in caplog.text


# --------------------------------------------------------------------------- #
# Slot Usage Limiter
# --------------------------------------------------------------------------- #


async def _setup_counter(hass: HomeAssistant, initial: int = 5) -> str:
    """Create an input_number counter helper and return its entity_id."""
    assert await async_setup_component(
        hass,
        "input_number",
        {
            "input_number": {
                "test_counter": {
                    "min": -1,
                    "max": 100,
                    "initial": initial,
                    "step": 1,
                }
            }
        },
    )
    await hass.async_block_till_done()
    return "input_number.test_counter"


async def test_limiter_decrements_on_pin_use(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Counter decrements by 1 each time the slot's PIN is used."""
    counter = await _setup_counter(hass, initial=3)

    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 2

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 1


async def test_limiter_does_not_spend_a_use_on_locking_by_default(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Locking up behind yourself does not cost a use.

    The entity records every use of the credential now, not just the
    unlocks it used to filter for, so without a filter here a guest who
    unlocks with their PIN and locks the door behind them with the same PIN
    would spend two of their uses -- on a 1-use code, locking themselves out
    for being polite. The blueprint's default counts ``unlock`` and
    ``unknown`` and not ``lock``, which is the contract this pins.
    """
    counter = await _setup_counter(hass, initial=3)

    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1, to_locked=False)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 2

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1, to_locked=True)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 2


async def test_limiter_spends_a_use_on_locking_when_widened(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Selecting ``lock`` makes every touch of the credential cost a use."""
    counter = await _setup_counter(hass, initial=3)

    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
            "counted_operations": ["unlock", "lock", "unknown"],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1, to_locked=True)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 2


async def test_limiter_spends_a_use_on_a_reported_use(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A use reported through the action still counts by default.

    ``use_credential`` reports ``unknown`` -- Lock Code Manager never sees
    what the device did next -- so a default that counted only ``unlock``
    would silently stop counting every external keypad use the action
    exists to report.
    """
    counter = await _setup_counter(hass, initial=3)

    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
        },
    )

    await _use_credential(hass, lock_code_manager_config_entry)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 2


async def test_notifier_runs_when_the_credential_locks_the_door(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    The notifier runs on locking by code and can say that is what happened.

    It has no operation filter on purpose -- a notification about somebody
    locking up is informative rather than harmful -- so the message is where
    the distinction has to be available, which is what ``operation`` and
    ``credential_type`` are for.
    """
    captured = async_mock_service(hass, "test", "captured")

    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [
                {
                    "service": "test.captured",
                    "data": {
                        "operation": "{{ operation }}",
                        "credential_type": "{{ credential_type }}",
                    },
                }
            ],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1, to_locked=True)
    await hass.async_block_till_done()

    assert len(captured) == 1
    assert captured[0].data["operation"] == "lock"
    assert captured[0].data["credential_type"] == "pin"


async def test_limiter_disables_switch_at_zero(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Reaching zero turns off the enabled switch (and clamps the counter)."""
    counter = await _setup_counter(hass, initial=1)
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")

    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert float(hass.states.get(counter).state) == 0
    assert len(turn_off_calls) == 1
    assert turn_off_calls[0].data["entity_id"] == [SLOT_1_ENABLED_ENTITY]


async def test_limiter_unlimited_does_not_decrement(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A counter set to -1 means unlimited — it stays at -1 forever."""
    counter = await _setup_counter(hass, initial=-1)
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")

    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert float(hass.states.get(counter).state) == -1
    assert turn_off_calls == []


async def test_limiter_lock_filter_blocks_non_match(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """PR #1153: uses on locks outside the configured set don't decrement."""
    counter = await _setup_counter(hass, initial=5)

    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
            "locks": [LOCK_1_ENTITY_ID],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_2_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 5

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 4


async def test_limiter_derives_slot_and_config_entry(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """PR #1160: notification title uses derived `_slot_number` and `_config_entry_title`.

    Pre-renders the title via a captured service so we can assert the derived
    template variables resolve to the expected values without needing a real
    notify integration.
    """
    counter = await _setup_counter(hass, initial=1)
    captured = async_mock_service(hass, "notify", "send_message")

    # Set up a fake notify entity so the blueprint's `notify_target != ''` check passes.
    hass.states.async_set("notify.test_target", "idle")

    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
            "notify_target": "notify.test_target",
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert len(captured) == 1
    title = captured[0].data["title"]
    # `_config_entry_title` ← config_entry_attr(config_entry_id(event_entity), 'title')
    # which is the LCM config entry's title ("Mock Title" per the test fixture).
    # `_slot_number` ← state_attr(event_entity, 'code_slot') = 1
    assert "Mock Title" in title
    assert "Slot 1" in title


# --------------------------------------------------------------------------- #
# Calendar Condition
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("condition_template", "expected_state", "expect_warning"),
    [
        ("{{ true }}", "on", False),
        ("{{ 'lock.front_door' in lock_entity_ids }}", "off", True),
    ],
    ids=["valid_template", "removed_lock_entity_ids"],
)
async def test_calendar_condition_fails_quietly_on_the_removed_variable(
    hass: HomeAssistant,
    caplog,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    condition_template: str,
    expected_state: str,
    expect_warning: bool,
) -> None:
    """
    A template still naming the removed ``lock_entity_ids`` reads ``off``.

    This is the claim the blueprint's description and BLUEPRINTS.md make to
    anybody upgrading, so it is pinned rather than reasoned about: Home
    Assistant renders an undefined template variable as empty instead of
    raising, which leaves the sensor available, reading ``off``, and the PIN
    it gates permanently inactive. The only signal is a template variable
    warning in the log. The valid-template case is what makes the ``off``
    mean something -- the same calendar, the same setup, reading ``on``.
    """
    calendar_entity = "calendar.guest_stay"
    hass.states.async_set(calendar_entity, "on", {"message": "Guest"})
    caplog.clear()

    with patch_blueprint(
        CALENDAR_PATH,
        BLUEPRINTS_ROOT / "template" / CALENDAR_PATH,
        schema=template_config.TEMPLATE_BLUEPRINT_SCHEMA,
    ):
        assert await async_setup_component(
            hass,
            "template",
            {
                "template": {
                    "name": "Guest Condition",
                    "use_blueprint": {
                        "path": CALENDAR_PATH,
                        "input": {
                            "config_entry": lock_code_manager_config_entry.entry_id,
                            "calendar_entity": calendar_entity,
                            "slot_number": 1,
                            "condition_template": condition_template,
                        },
                    },
                }
            },
        )
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.guest_condition")
    assert state is not None
    assert state.state == expected_state
    warned = (
        "Template variable warning" in caplog.text
        and "'lock_entity_ids' is undefined" in caplog.text
    )
    assert warned is expect_warning


@pytest.mark.parametrize(
    ("blueprint", "variable", "slot_field"),
    [
        ("template/lock_code_manager/calendar_condition.yaml", "_name_entity", "name"),
        (
            "automation/lock_code_manager/calendar_pin_setter.yaml",
            "pin_entity",
            "pin",
        ),
    ],
)
async def test_blueprints_find_the_entity_they_are_looking_for(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    blueprint: str,
    variable: str,
    slot_field: str,
) -> None:
    """
    Render the lookups out of the shipped YAML against a real setup.

    These templates resolve Lock Code Manager's own entities, and nothing else
    renders them -- which is how one of them shipped matching an entity ID
    suffix no entity has ever had. Reading the expression out of the file
    rather than restating it here means the test cannot drift from what
    users actually run.
    """
    source = yaml_util.parse_yaml(
        (BLUEPRINTS_ROOT / blueprint).read_text(encoding="utf-8")
    )
    expression = _find_variable(source, variable)
    assert expression, f"{blueprint} no longer defines {variable}"

    entry_id = lock_code_manager_config_entry.entry_id
    rendered = template.Template(expression, hass).async_render(
        variables={
            "_all_entities": template.Template(
                "{{ integration_entities(title) }}", hass
            ).async_render(variables={"title": lock_code_manager_config_entry.title}),
            "slot_number": 1,
        },
        parse_result=False,
    )

    assert rendered, f"{blueprint}:{variable} matched nothing"
    state = hass.states.get(rendered)
    assert state is not None
    assert state.attributes[ATTR_CODE_SLOT] == 1
    assert state.attributes[ATTR_SLOT_FIELD] == slot_field
    # Belongs to this entry, not merely to something named alike.
    assert er.async_get(hass).async_get(rendered).config_entry_id == entry_id


def _find_variable(source: dict, name: str) -> str | None:
    """Return the template a blueprint assigns to ``name``, wherever it sits."""
    if isinstance(source, dict):
        for key, value in source.items():
            if key == name and isinstance(value, str):
                return value
            if (found := _find_variable(value, name)) is not None:
                return found
    elif isinstance(source, list):
        for item in source:
            if (found := _find_variable(item, name)) is not None:
                return found
    return None


# --------------------------------------------------------------------------- #
# Credential Used
# --------------------------------------------------------------------------- #

CREDENTIAL_USED_PATH = "credential_used.yaml"


_CREDENTIAL_USED_INPUTS = {
    "credential_actions": [
        {
            "service": "test.captured",
            "data": {
                "name": "{{ name }}",
                "source": "{{ source }}",
                "target": "{{ target }}",
                "config_entry_id": "{{ config_entry_id }}",
                "config_entry_title": "{{ config_entry_title }}",
                "timestamp": "{{ timestamp }}",
            },
        }
    ],
}


async def _setup_credential_used(hass: HomeAssistant, **inputs) -> list[ServiceCall]:
    """Instantiate the credential-used blueprint and capture its actions."""
    captured = async_mock_service(hass, "test", "captured")
    await _setup_blueprint_automation(
        hass, CREDENTIAL_USED_PATH, {**inputs, **_CREDENTIAL_USED_INPUTS}
    )
    return captured


async def test_credential_used_exposes_the_whole_payload(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """With no filters set, any credential use runs the actions.

    Asserts every documented template variable at once, because the
    blueprint's contract with its users is the variable list -- one that
    stops rendering is a silent break in somebody's notification text.
    """
    captured = await _setup_credential_used(hass)

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert len(captured) == 1
    data = captured[0].data
    assert data["name"] == "test1"
    # A lock that observed the use is both ends of it.
    assert data["source"] == LOCK_1_ENTITY_ID
    assert data["target"] == LOCK_1_ENTITY_ID
    assert data["config_entry_id"] == lock_code_manager_config_entry.entry_id
    assert data["config_entry_title"] == lock_code_manager_config_entry.title
    assert data["timestamp"]


async def test_a_use_against_an_outside_target_reaches_both_blueprints(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A gate the entry knows nothing about notifies the user's blueprint too.

    The per-user event entity records every use of that user's credential
    whatever it acted on, so Slot Usage Notifier -- which triggers on that
    entity -- fires for a target no lock in the entry could ever be. This
    blueprint sees the same use straight off the bus, with the payload.

    Both automations are instantiated together because the automation
    domain only sets up once; separately, the second one would not exist.
    """
    captured = async_mock_service(hass, "test", "captured")
    notified = async_mock_service(hass, "test", "notified")
    await _setup_blueprint_automations(
        hass,
        (CREDENTIAL_USED_PATH, _CREDENTIAL_USED_INPUTS),
        (
            NOTIFIER_PATH,
            {
                "event_entity": [SLOT_1_EVENT_ENTITY],
                "notify_actions": [
                    {
                        "service": "test.notified",
                        "data": {
                            "slot_name": "{{ slot_name }}",
                            "lock_name": "{{ lock_name }}",
                        },
                    }
                ],
            },
        ),
    )

    await _use_credential(hass, lock_code_manager_config_entry)
    await hass.async_block_till_done()

    assert len(captured) == 1
    assert captured[0].data["name"] == "test1"
    assert captured[0].data["source"] == EXTERNAL_KEYPAD
    assert captured[0].data["target"] == EXTERNAL_TARGET

    assert len(notified) == 1
    assert notified[0].data["slot_name"] == "test1"
    # Nothing in Home Assistant holds a state for the gate, so the target's
    # own id is the most the notification can say about it.
    assert notified[0].data["lock_name"] == EXTERNAL_TARGET


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        pytest.param({"users": ["test1"]}, 1, id="users-match"),
        pytest.param({"users": ["someone-else"]}, 0, id="users-miss"),
        pytest.param({"sources": [EXTERNAL_KEYPAD]}, 1, id="sources-match"),
        pytest.param({"sources": [EXTERNAL_TARGET]}, 0, id="sources-crosswired"),
        pytest.param({"targets": [EXTERNAL_TARGET]}, 1, id="targets-match"),
        pytest.param({"targets": [EXTERNAL_KEYPAD]}, 0, id="targets-crosswired"),
    ],
)
async def test_credential_used_filters(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    inputs: dict,
    expected: int,
) -> None:
    """Each optional filter narrows on its own field and nothing else.

    Driven through ``use_credential`` rather than a lock-observed use so
    that source and target are DIFFERENT entities. A lock that saw the use
    itself is both ends of it, which would let a filter read the wrong
    field and still pass every case. The crosswired parameters feed each
    filter the other field's value and require a miss.
    """
    captured = await _setup_credential_used(hass, **inputs)

    await _use_credential(hass, lock_code_manager_config_entry)
    await hass.async_block_till_done()

    assert len(captured) == expected


@pytest.mark.parametrize("matches", [True, False], ids=["match", "miss"])
async def test_credential_used_config_entry_filter(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    matches: bool,
) -> None:
    """The config entry filter narrows to one configuration.

    Held apart from the list filters because it is a scalar with an empty
    string rather than an empty list for "no filter".
    """
    entry_id = lock_code_manager_config_entry.entry_id
    captured = await _setup_credential_used(
        hass, config_entry=entry_id if matches else "some_other_entry"
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()

    assert len(captured) == (1 if matches else 0)


async def test_limiter_ignores_recovery_from_unavailable(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Coming back from `unavailable` is not a use.

    The state object still carries the LAST recorded use's attributes when
    the entity returns, so an `operation` filter alone cannot tell a
    recovery from a fresh use -- it reads the old use's operation and
    passes. Nobody entered a code here, so nothing may be spent.
    """
    counter = await _setup_counter(hass, initial=3)
    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1, to_locked=False)
    await hass.async_block_till_done()
    assert float(hass.states.get(counter).state) == 2

    # Driven directly rather than through lock availability: this pins the
    # blueprint's contract about the transition, whatever produced it.
    recorded = hass.states.get(SLOT_1_EVENT_ENTITY)
    hass.states.async_set(SLOT_1_EVENT_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    hass.states.async_set(
        SLOT_1_EVENT_ENTITY, recorded.state, dict(recorded.attributes)
    )
    await hass.async_block_till_done()

    assert float(hass.states.get(counter).state) == 2


async def test_limiter_ignores_first_appearance(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    An entity appearing for the first time is not a use.

    After a restart or a reload the entity is restored with the last use it
    recorded, which arrives as a transition from nothing. The notifier
    guards `from_state is None`; the limiter has to as well or every reload
    spends a use.
    """
    counter = await _setup_counter(hass, initial=3)
    await _setup_blueprint_automation(
        hass,
        LIMITER_PATH,
        {
            "pin_used_entity": SLOT_1_EVENT_ENTITY,
            "enabled_switch": SLOT_1_ENABLED_ENTITY,
            "uses_counter": counter,
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1, to_locked=False)
    await hass.async_block_till_done()
    recorded = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert float(hass.states.get(counter).state) == 2

    hass.states.async_remove(SLOT_1_EVENT_ENTITY)
    await hass.async_block_till_done()
    hass.states.async_set(
        SLOT_1_EVENT_ENTITY, recorded.state, dict(recorded.attributes)
    )
    await hass.async_block_till_done()

    assert float(hass.states.get(counter).state) == 2


async def test_reported_use_is_visible_while_every_lock_is_down(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A use reported while no lock is reachable still reaches consumers.

    ``use_credential`` exists for uses this integration cannot observe, and
    it refuses nothing when the locks are unreachable -- so gating the
    entity that carries those uses on lock reachability hides them exactly
    when the mechanism is most likely to be in use. It hides them twice
    over: the use arrives later as an ``unavailable -> <timestamp>``
    transition, which consumers deliberately discard as lock recovery.
    """
    captured = async_mock_service(hass, "test", "captured")
    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured", "data": {}}],
        },
    )

    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        hass.states.async_set(lock_entity_id, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    await _use_credential(hass, lock_code_manager_config_entry)
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state is not None
    assert state.state != STATE_UNAVAILABLE
    assert len(captured) == 1


async def test_lock_recovery_notifies_nobody(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A lock coming back is not a use, and must reach no consumer as one.

    This is the property the entity's old lock-following availability was
    protecting, kept after that availability was removed: the guarantee is
    now that a recovering lock produces no transition on this entity at
    all, rather than one the blueprints have to recognise and discard.
    """
    captured = async_mock_service(hass, "test", "captured")
    await _setup_blueprint_automation(
        hass,
        NOTIFIER_PATH,
        {
            "event_entity": [SLOT_1_EVENT_ENTITY],
            "notify_actions": [{"service": "test.captured", "data": {}}],
        },
    )

    _fire_pin_used(lock_code_manager_config_entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert len(captured) == 1

    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        hass.states.async_set(lock_entity_id, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        hass.states.async_set(lock_entity_id, "locked")
    await hass.async_block_till_done()

    assert len(captured) == 1
