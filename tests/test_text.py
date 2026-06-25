"""Test text platform."""

import logging
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

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

    async def _get_cached_capabilities() -> LockCapabilities | None:
        """Stand in for the async probe the add hook runs in the background."""
        return caps

    return SimpleNamespace(
        cached_capabilities=caps,
        lock=SimpleNamespace(entity_id=entity_id),
        _get_cached_capabilities=_get_cached_capabilities,
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


def test_pin_max_reflects_single_lock(hass: HomeAssistant) -> None:
    """A lock advertising 4-8 surfaces a max of 8; the min stays permissive."""
    entity = _make_text_entity(hass, CONF_PIN, [_fake_lock("lock.a", _pin_caps(4, 8))])
    assert (entity.native_min, entity.native_max) == (0, 8)


def test_pin_max_takes_tightest_common(hass: HomeAssistant) -> None:
    """Two locks collapse to the smallest advertised maximum."""
    entity = _make_text_entity(
        hass,
        CONF_PIN,
        [_fake_lock("lock.a", _pin_caps(4, 8)), _fake_lock("lock.b", _pin_caps(6, 10))],
    )
    assert (entity.native_min, entity.native_max) == (0, 8)


def test_native_min_stays_zero_despite_advertised_minimum(hass: HomeAssistant) -> None:
    """The advertised minimum is never surfaced as a hard floor.

    Surfacing it would make HA's text service reject the empty string that
    clears a slot; the coordinator owns the minimum instead.
    """
    entity = _make_text_entity(hass, CONF_PIN, [_fake_lock("lock.a", _pin_caps(6, 8))])
    assert entity.native_min == 0
    assert entity.native_max == 8


def test_native_max_widens_to_admit_longer_stored_value(hass: HomeAssistant) -> None:
    """A stored PIN longer than the advertised max still renders (ceiling widens).

    HA raises at state-render time if the value exceeds native_max, so a PIN
    written before a (now tighter) lock advertised its limit forces the ceiling
    up to admit it.
    """
    entity = _make_text_entity(hass, CONF_PIN, [_fake_lock("lock.a", _pin_caps(4, 8))])
    with patch.object(
        LockCodeManagerText,
        "native_value",
        new_callable=PropertyMock,
        return_value="1234567890",
    ):
        assert entity.native_min == 0
        assert entity.native_max == 10  # widened up to admit the length-10 value


def test_name_entity_ignores_capabilities(hass: HomeAssistant) -> None:
    """The name entity is not a credential key; bounds stay 0/9999."""
    entity = _make_text_entity(hass, CONF_NAME, [_fake_lock("lock.a", _pin_caps(4, 8))])
    assert (entity.native_min, entity.native_max) == (0, 9999)


async def test_lock_add_remove_rewrites_state(hass: HomeAssistant) -> None:
    """Lock set changes re-push state so the frontend re-reads bounds."""
    entity = _make_text_entity(hass, CONF_PIN, [])
    entity.hass = hass
    entity.entity_id = "text.test"

    with patch.object(entity, "async_write_ha_state") as mock_write:
        added = _fake_lock("lock.a", _pin_caps(4, 8))
        entity._handle_add_locks([added])
        await hass.async_block_till_done()
        assert added in entity.locks
        # The added lock's max is surfaced; the min stays permissive.
        assert (entity.native_min, entity.native_max) == (0, 8)
        assert mock_write.called  # immediate re-push, plus one after the probe
        mock_write.reset_mock()

        entity._handle_remove_lock("lock.a")
        await hass.async_block_till_done()
        assert entity.locks == []
        # Removing the only lock reverts the surfaced ceiling to the default.
        assert (entity.native_min, entity.native_max) == (0, 9999)
        assert mock_write.called  # the removal re-pushed state


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
    # The minimum is owned by the coordinator, not surfaced as a hard floor.
    assert state.attributes[ATTR_MIN] == 0
    assert state.attributes[ATTR_MAX] == 8


async def test_pin_clear_through_service_with_minimum_advertised(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Clearing a PIN via text.set_value works even when locks advertise a minimum.

    Regression: surfacing the advertised minimum as ``native_min`` made HA's
    text service reject the empty string (``len 0 < min``) before the
    coordinator's empty-PIN exemption ran, so a slot could not be cleared.
    """
    for lock in lock_code_manager_config_entry.runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(6, 8)

    # An in-range PIN goes through the service normally.
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "654321"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )
    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "654321"

    # Clearing must reach the coordinator (empty is exempt) rather than being
    # rejected by HA's service-level minimum check.
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
