"""Test text platform."""

import logging
from types import SimpleNamespace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.text import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
    TextMode,
)
from homeassistant.const import ATTR_ENTITY_ID, CONF_NAME, CONF_PIN, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.credentials import (
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
)
from custom_components.lock_code_manager.domain.models import (
    LockCodeManagerConfigEntryRuntimeData,
)
from custom_components.lock_code_manager.text import (
    CREDENTIAL_TYPE_BY_CONF_KEY,
    LockCodeManagerText,
)

from .common import SLOT_2_ENABLED_ENTITY, SLOT_2_NAME_ENTITY, SLOT_2_PIN_ENTITY

_LOGGER = logging.getLogger(__name__)


def _pin_caps(min_length: int, max_length: int) -> LockCapabilities:
    """Build LockCapabilities advertising a PIN type with the given bounds."""
    return LockCapabilities(
        supports_user_management=True,
        max_users=30,
        credential_types={
            CredentialType.PIN: CredentialTypeCapability(
                num_slots=30,
                min_length=min_length,
                max_length=max_length,
                supports_learn=False,
            )
        },
    )


def _fake_lock(entity_id: str, caps: LockCapabilities | None):
    """A stand-in lock exposing only what the text entity reads."""
    return SimpleNamespace(
        cached_capabilities=caps, lock=SimpleNamespace(entity_id=entity_id)
    )


def _make_text_entity(
    hass: HomeAssistant, key: str, locks: list
) -> LockCodeManagerText:
    """Construct a text entity with a controlled lock list for bounds tests."""
    config_entry = MockConfigEntry(domain=DOMAIN, title="Test")
    config_entry.add_to_hass(hass)
    config_entry.runtime_data = LockCodeManagerConfigEntryRuntimeData()

    entity = LockCodeManagerText(
        hass,
        er.async_get(hass),
        config_entry,
        1,
        key,
        TextMode.PASSWORD if key == CONF_PIN else TextMode.TEXT,
    )
    entity.locks = locks
    return entity


def test_conf_key_credential_type_map() -> None:
    """The map exposes PIN and excludes the (non-credential) name key."""
    assert CREDENTIAL_TYPE_BY_CONF_KEY[CONF_PIN] is CredentialType.PIN
    assert CONF_NAME not in CREDENTIAL_TYPE_BY_CONF_KEY


def test_pin_bounds_default_without_capabilities(hass: HomeAssistant) -> None:
    """An uncached lock contributes no constraint; bounds stay 0/9999."""
    entity = _make_text_entity(hass, CONF_PIN, [_fake_lock("lock.a", None)])
    assert (entity.native_min, entity.native_max) == (0, 9999)


def test_pin_bounds_reflect_single_lock(hass: HomeAssistant) -> None:
    """A lock advertising 4-8 sizes the PIN entity to 4-8."""
    entity = _make_text_entity(hass, CONF_PIN, [_fake_lock("lock.a", _pin_caps(4, 8))])
    assert (entity.native_min, entity.native_max) == (4, 8)


def test_pin_bounds_take_tightest_common(hass: HomeAssistant) -> None:
    """Two locks collapse to the largest min and smallest max."""
    entity = _make_text_entity(
        hass,
        CONF_PIN,
        [_fake_lock("lock.a", _pin_caps(4, 8)), _fake_lock("lock.b", _pin_caps(6, 10))],
    )
    assert (entity.native_min, entity.native_max) == (6, 8)


def test_pin_bounds_fall_back_on_empty_intersection(hass: HomeAssistant) -> None:
    """Unsatisfiable across locks -> default range, not an inverted slider."""
    entity = _make_text_entity(
        hass,
        CONF_PIN,
        [_fake_lock("lock.a", _pin_caps(6, 6)), _fake_lock("lock.b", _pin_caps(4, 4))],
    )
    assert (entity.native_min, entity.native_max) == (0, 9999)


def test_pin_bounds_admit_empty_value_under_minimum(
    hass: HomeAssistant, monkeypatch
) -> None:
    """An empty PIN must always render even when the lock requires a minimum.

    HA raises at state-render time if the value is shorter than the min, so a
    cleared PIN ("") forces the advertised minimum down to 0.
    """
    entity = _make_text_entity(hass, CONF_PIN, [_fake_lock("lock.a", _pin_caps(6, 8))])
    monkeypatch.setattr(LockCodeManagerText, "native_value", property(lambda self: ""))
    assert entity.native_min == 0
    assert entity.native_max == 8


def test_pin_bounds_admit_out_of_range_current_value(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A stored PIN outside the advertised range still renders (bounds widen)."""
    entity = _make_text_entity(hass, CONF_PIN, [_fake_lock("lock.a", _pin_caps(6, 8))])
    monkeypatch.setattr(
        LockCodeManagerText, "native_value", property(lambda self: "1234")
    )
    assert entity.native_min == 4  # widened down to admit the length-4 value
    assert entity.native_max == 8


def test_name_entity_ignores_capabilities(hass: HomeAssistant) -> None:
    """The name entity is not a credential key; bounds stay 0/9999."""
    entity = _make_text_entity(hass, CONF_NAME, [_fake_lock("lock.a", _pin_caps(4, 8))])
    assert (entity.native_min, entity.native_max) == (0, 9999)


def test_lock_add_remove_rewrites_state(hass: HomeAssistant, monkeypatch) -> None:
    """Lock set changes re-push state so the frontend re-reads bounds."""
    entity = _make_text_entity(hass, CONF_PIN, [])
    entity.hass = hass
    entity.entity_id = "text.test"
    writes: list[int] = []
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: writes.append(1))

    added = _fake_lock("lock.a", _pin_caps(4, 8))
    entity._handle_add_locks([added])
    assert added in entity.locks
    assert (entity.native_min, entity.native_max) == (4, 8)
    assert len(writes) == 1

    entity._handle_remove_lock("lock.a")
    assert entity.locks == []
    assert len(writes) == 2


async def test_pin_entity_surfaces_lock_bounds(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The PIN entity's state exposes the bound lock's length range."""
    # Without advertised capabilities the entity uses the default range.
    # Home Assistant clamps the reported max to its 255-char state ceiling.
    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.attributes[ATTR_MIN] == 0
    assert state.attributes[ATTR_MAX] == 255

    # Warm the lock's capability cache, then a write re-reads the bounds.
    for lock in lock_code_manager_config_entry.runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(4, 8)

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "1234"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.attributes[ATTR_MIN] == 4
    assert state.attributes[ATTR_MAX] == 8


async def test_text_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test text entities."""
    state = hass.states.get(SLOT_2_NAME_ENTITY)
    assert state
    assert state.state == "test2"

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "5678"

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0987"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "0987"

    # Clearing a PIN on an enabled slot should auto-disable the slot and clear the PIN
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: ""},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == ""

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_OFF


async def test_whitespace_pin_normalized_to_empty(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that a whitespace-only PIN is normalized to empty and auto-disables the slot."""
    # First verify the slot is enabled and has a PIN
    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "5678"

    # Set a whitespace-only PIN — should normalize to "" and auto-disable
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "   "},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == ""

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_OFF
