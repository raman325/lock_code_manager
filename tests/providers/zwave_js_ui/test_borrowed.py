"""A provider built to answer one question must not outlive the answer."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from custom_components.lock_code_manager.domain.allocation import (
    async_check_slot_capacity,
    async_max_slot,
    async_read_occupancy,
)
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.unmanaged import (
    async_sweep_unmanaged_codes,
)
from custom_components.lock_code_manager.providers.zwave_js_ui import (
    ZWaveJSUILock,
)

from .conftest import (
    ZWaveJSUIApiResponder,
    fire_zui_node_value,
    track_zui_transport,
)

# zwave-js UserIDStatus: 0 Available.
SLOT_IS_EMPTY = {"userIdStatus": 0}
LOCK_CAPACITY = 30
KEYPAD_UNLOCK = "113/0/Access_Control/Keypad_unlock_operation"


@pytest.fixture
def zui_lock_answering(
    zui_api_responder: ZWaveJSUIApiResponder,
    zui_lock_discovered: er.RegistryEntry,
) -> str:
    """
    Return the entity id of a discovered lock whose gateway answers reads.

    Every question the allocation layer asks a zwave-js-ui lock is one
    ``sendCommand``, so one handler covers occupancy, capability, and slot
    range alike.
    """

    def _handler(_api_base: str, request: dict) -> dict:
        _target, method, _args = request["args"]
        return {
            "success": True,
            "message": "",
            "result": LOCK_CAPACITY if method == "getUsersCount" else SLOT_IS_EMPTY,
        }

    zui_api_responder.set_handler("sendCommand", _handler)
    return zui_lock_discovered.entity_id


async def test_the_occupancy_read_returns_the_transport_it_borrowed(
    hass: HomeAssistant, zui_lock_answering: str
) -> None:
    """
    Reading what a lock holds opens its transport, and closes it again.

    The answer is asserted too: a provider that could not reach the lock
    would leak nothing either, and the reason this read exists is that
    allocation refuses to issue numbers it could not verify are free.
    """
    with track_zui_transport() as transport:
        occupancy = await async_read_occupancy(
            hass, None, [zui_lock_answering], range(1, 3)
        )

    assert occupancy.is_known
    assert transport.opened
    assert transport.live == []
    assert transport.disposed == [zui_lock_answering]


async def test_a_lock_that_cannot_be_read_is_unknown_rather_than_free(
    hass: HomeAssistant, zui_lock_answering: str
) -> None:
    """
    A read that raises leaves the lock unknown, and the transport still closes.

    The two failure directions are not symmetric: refusing to issue a number
    blocks the user from adding somebody, but issuing an occupied one
    overwrites a credential on a real door. So an unreadable lock is treated
    as unknown, never as empty -- and the borrowed transport is disposed of on
    the way out regardless, or every config flow that hit a sulking lock would
    leak one.
    """
    with (
        patch.object(
            ZWaveJSUILock,
            "async_internal_get_occupied_indices",
            side_effect=LockDisconnected("no answer"),
        ),
        track_zui_transport() as transport,
    ):
        occupancy = await async_read_occupancy(
            hass, None, [zui_lock_answering], range(1, 3)
        )

    assert not occupancy.is_known
    assert transport.disposed == [zui_lock_answering]


async def test_the_capacity_check_returns_the_transport_it_borrowed(
    hass: HomeAssistant, zui_lock_answering: str
) -> None:
    """
    The capability probe is disposed of like any other borrowed query.

    Nothing is asserted about subscriptions here because zwave-js-ui answers
    this one without opening any: the point is that the site disposes
    regardless, so a provider that starts answering it over its transport
    does not have to remember to add a teardown.
    """
    with track_zui_transport() as transport:
        await async_check_slot_capacity(hass, None, [zui_lock_answering], [2])

    assert transport.disposed == [zui_lock_answering]


async def test_the_slot_range_query_returns_the_transport_it_borrowed(
    hass: HomeAssistant, zui_lock_answering: str
) -> None:
    """Asking how far the slot numbers go opens the same transport, and closes it."""
    with track_zui_transport() as transport:
        assert await async_max_slot(hass, None, [zui_lock_answering]) == (
            LOCK_CAPACITY,
            zui_lock_answering,
        )

    assert transport.opened
    assert transport.live == []
    assert transport.disposed == [zui_lock_answering]


async def test_the_unmanaged_sweep_returns_the_transport_it_borrowed(
    hass: HomeAssistant, zui_lock_answering: str
) -> None:
    """The migration's one-off sweep builds its own providers and owns their teardown."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [zui_lock_answering], CONF_SLOTS: {}},
        unique_id="test_zui_sweep",
    )
    entry.add_to_hass(hass)

    with track_zui_transport() as transport:
        await async_sweep_unmanaged_codes(hass, entry)

    assert transport.opened
    assert transport.live == []
    assert transport.disposed == [zui_lock_answering]


async def test_a_returned_provider_stops_reporting_keypad_use(
    hass: HomeAssistant,
    zui_api_responder: ZWaveJSUIApiResponder,
    zui_lock: ZWaveJSUILock,
) -> None:
    """
    A borrowed provider's node subscription must not double the entry's events.

    Home Assistant hands an arriving message to every matching subscription,
    so a throwaway left subscribed reports the same keypad press a second
    time, from an instance nothing owns -- once more for every config flow
    the user opens.
    """
    zui_api_responder.set_result("sendCommand", SLOT_IS_EMPTY)
    events: list[Event] = []
    hass.bus.async_listen(EVENT_LOCK_STATE_CHANGED, events.append)

    fire_zui_node_value(hass, KEYPAD_UNLOCK, {"userId": 1})
    await hass.async_block_till_done()
    assert [event.data[ATTR_CODE_SLOT] for event in events] == [1]

    # What an options flow does on its way to re-numbering the users.
    await async_read_occupancy(hass, None, [zui_lock.lock.entity_id], range(1, 3))

    fire_zui_node_value(hass, KEYPAD_UNLOCK, {"userId": 1})
    await hass.async_block_till_done()
    assert [event.data[ATTR_CODE_SLOT] for event in events] == [1, 1]
