"""Behavioral tests for the shipped Lock Code Manager blueprints.

Each blueprint is loaded via ``patch_blueprint`` so the real YAML on disk is
exercised end-to-end: trigger fires from a real LCM event entity, the
automation runs, and we assert on the side effects via mocked services.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import pathlib
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache_with_extra_data,
)

from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import Event, HomeAssistant, ServiceCall, State, callback
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.providers import BaseLock

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_EVENT_ENTITY,
    SLOT_2_EVENT_ENTITY,
)

BLUEPRINT_FOLDER = (
    pathlib.Path(__file__).resolve().parent.parent
    / "blueprints"
    / "automation"
    / "lock_code_manager"
)

NOTIFIER_PATH = "slot_usage_notifier.yaml"
LIMITER_PATH = "slot_usage_limiter.yaml"


@contextlib.contextmanager
def patch_blueprint(blueprint_path: str, data_path: pathlib.Path) -> Iterator[None]:
    """Intercept blueprint loading so HA reads our repo YAML directly.

    Mirrors the helper used in homeassistant.tests.components.automation.test_blueprint.
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
            schema=automation.config.AUTOMATION_BLUEPRINT_SCHEMA,
        )

    with patch(
        "homeassistant.components.blueprint.models.DomainBlueprints._load_blueprint",
        mock_load,
    ):
        yield


async def _setup_blueprint_automation(
    hass: HomeAssistant, blueprint_path: str, inputs: dict
) -> None:
    """Instantiate a blueprint-based automation with the given inputs."""
    with patch_blueprint(blueprint_path, BLUEPRINT_FOLDER / blueprint_path):
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "use_blueprint": {"path": blueprint_path, "input": inputs}
                }
            },
        )
    await hass.async_block_till_done()


def _fire_pin_used(
    config_entry, lock_entity_id: str, slot: int, action_text: str = "test"
) -> None:
    """Fire a PIN-used event from the named lock on the named slot."""
    lock: BaseLock = config_entry.runtime_data.locks[lock_entity_id]
    lock.async_fire_code_slot_event(slot, False, action_text, Event("test_source"))


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
