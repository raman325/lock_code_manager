"""Test per-lock naming."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.model.locks import lock_display_name


async def test_lock_display_name_prefers_the_name_on_screen(
    hass: HomeAssistant,
) -> None:
    """The friendly name is the string a person would use to say which lock."""
    hass.states.async_set("lock.front_door", "locked", {"friendly_name": "Front Door"})

    assert lock_display_name(hass, "lock.front_door") == "Front Door"


async def test_lock_display_name_falls_back_to_the_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """
    A lock named by its device is still named.

    This is the shape Z-Wave, Matter and most modern integrations use:
    ``has_entity_name`` with no name of its own, so ``original_name`` is
    empty and the whole visible name comes from the device. Reading the
    registry row alone names such a lock ``lock.front_door``, which is
    what every per-lock entity was called.
    """
    entry = MockConfigEntry(domain="zwave_js")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("zwave_js", "node-3")},
        name="Front Door",
    )
    entity = entity_registry.async_get_or_create(
        "lock",
        "zwave_js",
        "node-3-98-0-currentMode",
        device_id=device.id,
        original_name="",
        suggested_object_id="front_door",
    )
    assert entity.entity_id == "lock.front_door"

    assert lock_display_name(hass, "lock.front_door") == "Front Door"


async def test_lock_display_name_prefers_a_renamed_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """What the person called it beats what the integration called it."""
    entry = MockConfigEntry(domain="zwave_js")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("zwave_js", "node-4")},
        name="Schlage BE469",
    )
    device_registry.async_update_device(device.id, name_by_user="Back Door")
    entity_registry.async_get_or_create(
        "lock",
        "zwave_js",
        "node-4-98-0-currentMode",
        device_id=device.id,
        original_name="",
        suggested_object_id="back_door",
    )

    assert lock_display_name(hass, "lock.back_door") == "Back Door"


async def test_lock_display_name_uses_the_entity_name_when_it_has_one(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """An integration that names its lock entity is taken at its word."""
    entity_registry.async_get_or_create(
        "lock",
        "demo",
        "somelock",
        original_name="Side Gate",
        suggested_object_id="side_gate",
    )

    assert lock_display_name(hass, "lock.side_gate") == "Side Gate"


async def test_lock_display_name_falls_back_to_the_object_id(
    hass: HomeAssistant,
) -> None:
    """Nothing left to ask: the object id is what the entity id was slugged from."""
    assert lock_display_name(hass, "lock.garage_side") == "garage side"
