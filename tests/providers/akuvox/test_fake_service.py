"""Self-tests for the stateful Akuvox device (``fake_service.py``), against local_akuvox."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.providers.akuvox import AKUVOX_DOMAIN

from .fake_service import FakeAkuvoxDevice


async def _call(
    hass: HomeAssistant, entity_id: str, service: str, **data
) -> dict | None:
    return await hass.services.async_call(
        AKUVOX_DOMAIN,
        service,
        data,
        target={"entity_id": entity_id},
        blocking=True,
        return_response=service == "list_users",
    )


async def test_list_users_answers_every_user_field(
    hass: HomeAssistant,
    fake_akuvox_device: FakeAkuvoxDevice,
    akuvox_lock_entity: er.RegistryEntry,
) -> None:
    """AL:872-916 answers vars(User) (PU:19-32), per entity."""
    entity_id = akuvox_lock_entity.entity_id
    await _call(
        hass,
        entity_id,
        "add_user",
        name="Mom",
        private_pin="1111",
        schedules=["1001", "1002"],
        lift_floor_num="1",
    )

    [user] = (await _call(hass, entity_id, "list_users"))[entity_id]["users"]

    assert set(user) == {
        "name",
        "user_id",
        "schedule_relay",
        "id",
        "web_relay",
        "private_pin",
        "card_code",
        "lift_floor_num",
        "user_type",
        "source",
        "source_type",
    }
    assert (user["name"], user["private_pin"], user["schedule_relay"]) == (
        "Mom",
        "1111",
        "1001-1,1002-1",
    )


@pytest.mark.parametrize("pin", ["123", "123456789", "12a4"])
async def test_a_pin_must_be_four_to_eight_digits(
    hass: HomeAssistant,
    fake_akuvox_device: FakeAkuvoxDevice,
    akuvox_lock_entity: er.RegistryEntry,
    pin: str,
) -> None:
    """AV:53-66."""
    with pytest.raises(ServiceValidationError, match="PIN must be 4-8 digits"):
        await _call(
            hass,
            akuvox_lock_entity.entity_id,
            "add_user",
            name="Mom",
            private_pin=pin,
            schedules=["1001"],
            lift_floor_num="1",
        )


async def test_a_user_not_there_cannot_be_changed(
    hass: HomeAssistant,
    fake_akuvox_device: FakeAkuvoxDevice,
    akuvox_lock_entity: er.RegistryEntry,
) -> None:
    """AL:1199-1202."""
    with pytest.raises(HomeAssistantError, match="delete_user: user '9' not found"):
        await _call(hass, akuvox_lock_entity.entity_id, "delete_user", id="9")


async def test_a_cloud_user_cannot_be_changed(
    hass: HomeAssistant,
    fake_akuvox_device: FakeAkuvoxDevice,
    akuvox_lock_entity: er.RegistryEntry,
) -> None:
    """AL:1204-1207 with AV:69-89: a cloud-provisioned user is refused."""
    user = fake_akuvox_device.add("Cloud user", "2222", source_type="2")

    with pytest.raises(ServiceValidationError, match="cloud-provisioned"):
        await _call(
            hass, akuvox_lock_entity.entity_id, "modify_user", id=user, name="x"
        )


async def test_ignore_acknowledges_and_changes_nothing(
    hass: HomeAssistant,
    fake_akuvox_device: FakeAkuvoxDevice,
    akuvox_lock_entity: er.RegistryEntry,
) -> None:
    """A device that drops a write it acknowledged."""
    user = fake_akuvox_device.add("Mom", "1111")
    fake_akuvox_device.ignore("delete_user")

    await _call(hass, akuvox_lock_entity.entity_id, "delete_user", id=user)

    assert fake_akuvox_device.pin_of("Mom") == "1111"


async def test_an_ignore_for_one_call_lets_the_next_through(
    hass: HomeAssistant,
    fake_akuvox_device: FakeAkuvoxDevice,
    akuvox_lock_entity: er.RegistryEntry,
) -> None:
    """``times`` spends the fault; ``hits`` counts the calls it met."""
    user = fake_akuvox_device.add("Mom", "1111")
    fault = fake_akuvox_device.ignore("delete_user", times=1)

    await _call(hass, akuvox_lock_entity.entity_id, "delete_user", id=user)
    assert fake_akuvox_device.pin_of("Mom") == "1111"
    await _call(hass, akuvox_lock_entity.entity_id, "delete_user", id=user)

    assert fake_akuvox_device.pin_of("Mom") is None
    assert fault.hits == 1
