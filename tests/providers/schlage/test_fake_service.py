"""Self-tests for the stateful Schlage cloud (``fake_service.py``), against core's code services."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
import pytest
from requests.exceptions import ReadTimeout

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.providers.schlage import SCHLAGE_DOMAIN

from .fake_service import FakeSchlageCloud


async def _call(hass: HomeAssistant, service: str, **data: str) -> dict | None:
    return await hass.services.async_call(
        SCHLAGE_DOMAIN,
        service,
        data,
        blocking=True,
        return_response=service == "get_codes",
    )


async def test_get_codes_answers_per_entity(
    hass: HomeAssistant,
    fake_schlage_cloud: FakeSchlageCloud,
    schlage_lock_entity: er.RegistryEntry,
) -> None:
    """SL:165-177, wrapped per entity as an entity service answers."""
    code_id = fake_schlage_cloud.add("Mom", "1111")

    response = await _call(hass, "get_codes", entity_id=schlage_lock_entity.entity_id)

    assert response == {
        schlage_lock_entity.entity_id: {code_id: {"name": "Mom", "code": "1111"}}
    }


@pytest.mark.parametrize(
    ("name", "code", "message"),
    [
        (
            " mom ",
            "2222",
            'A PIN code with the name " mom " already exists on the lock',
        ),
        ("Dad", "1111", "A PIN code with this value already exists on the lock"),
    ],
    ids=["name", "code"],
)
async def test_add_refuses_a_name_or_code_already_there(
    hass: HomeAssistant,
    fake_schlage_cloud: FakeSchlageCloud,
    schlage_lock_entity: er.RegistryEntry,
    name: str,
    code: str,
    message: str,
) -> None:
    """SL:74-104: names compare case- and space-insensitively; the message is core's."""
    fake_schlage_cloud.add("Mom", "1111")

    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "add_code",
            entity_id=schlage_lock_entity.entity_id,
            name=name,
            code=code,
        )

    assert str(err.value) == message
    assert len(fake_schlage_cloud.codes) == 1


async def test_deleting_a_name_not_there_succeeds(
    hass: HomeAssistant,
    fake_schlage_cloud: FakeSchlageCloud,
    schlage_lock_entity: er.RegistryEntry,
) -> None:
    """SL:150-154: a code not found is a successful delete."""
    await _call(
        hass, "delete_code", entity_id=schlage_lock_entity.entity_id, name="Nobody"
    )


async def test_read_lag_answers_the_codes_from_before_a_write(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    fake_schlage_cloud: FakeSchlageCloud,
    schlage_lock_entity: er.RegistryEntry,
) -> None:
    """For ``read_lag`` seconds the cloud reads back what it held before (#1083)."""
    fake_schlage_cloud.read_lag = 30
    entity_id = schlage_lock_entity.entity_id

    async def _count() -> int:
        return len((await _call(hass, "get_codes", entity_id=entity_id))[entity_id])

    await _call(hass, "add_code", entity_id=entity_id, name="Mom", code="1111")
    reads = [await _count()]
    freezer.tick(20)
    # A second write inside the lag keeps reads on the table before the first.
    await _call(hass, "add_code", entity_id=entity_id, name="Dad", code="2222")
    reads.append(await _count())
    freezer.tick(29)
    reads.append(await _count())
    freezer.tick(2)
    reads.append(await _count())

    assert reads == [0, 0, 0, 2]


async def test_a_timeout_is_the_raw_requests_error(
    hass: HomeAssistant,
    fake_schlage_cloud: FakeSchlageCloud,
    schlage_lock_entity: er.RegistryEntry,
) -> None:
    """SL:106-115 translates only SchlageError; a ReadTimeout (OSError) escapes."""
    fake_schlage_cloud.time_out("get_codes")

    with pytest.raises(ReadTimeout):
        await _call(hass, "get_codes", entity_id=schlage_lock_entity.entity_id)
    await _call(hass, "get_codes", entity_id=schlage_lock_entity.entity_id)
