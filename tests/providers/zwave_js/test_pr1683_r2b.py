"""Adversarial review of PR #1683 (round 2b): user-facing behaviour on a real Z-Wave JS lock."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.domain.queries import subentry_id_for_slot
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_set_slot_pin,
    async_switch_slot,
    in_sync_entity_id,
    lcm_issue_ids,
    slot_entity_id,
    user_subentries,
)
from tests.providers.zwave_js.conftest import ZWAVE_JS_LCM_CONFIG_SLOTS
from tests.providers.zwave_js.fake_node import (
    USER_CREDENTIAL_CC_ID,
    FakeZWaveLock,
    user_credential_lock_state,
)


def _switch(hass: HomeAssistant, entry: MockConfigEntry, slot: int) -> str:
    return hass.states.get(
        slot_entity_id(hass, SWITCH_DOMAIN, entry, slot, CONF_ENABLED)
    ).state


def _disabled_reason(hass: HomeAssistant, entry: MockConfigEntry, slot: int) -> str:
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"slot_disabled_{entry.entry_id}_{slot}"
    )
    assert issue is not None
    return issue.translation_placeholders["reason"]


class TestDuplicates:
    """The restored duplicate refusal, met through the real Z-Wave JS stack."""

    @pytest.fixture
    def fake_zwave_lock_options(self) -> dict[str, Any]:
        """The lock holds the entry's PINs, and the homeowner's 5555 at slot 5."""
        return {"codes": {1: "9999", 2: "1234", 5: "5555"}}

    async def _settle(self, hass, freezer, entry, lock_entity) -> None:
        await async_advance_sync_ticks(hass, freezer, 5)
        for slot in (1, 2):
            entity_id = in_sync_entity_id(hass, entry, slot, lock_entity.entity_id)
            assert hass.states.get(entity_id).state == STATE_ON

    async def test_the_homeowner_s_code_refusal_says_which_slot_has_it(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        The repair says why the slot was turned off: a duplicate, and of what.

        Before the swap it read "... has been disabled. Lock ...: cannot set
        code on slot 2 -- PIN duplicates unmanaged slot 5". Now it says only
        that the lock "refused" the PIN: the user re-enables it, it is turned
        off again, and nothing says the homeowner's own code is in the way.
        """
        entry = fake_zwave_lock_entry
        await self._settle(hass, freezer, entry, lock_entity)

        await async_set_slot_pin(hass, entry, 2, "5555")
        await async_advance_sync_ticks(hass, freezer, 5)

        assert _switch(hass, entry, 2) == STATE_OFF
        reason = _disabled_reason(hass, entry, 2)
        assert "duplicate" in reason.lower()
        assert "slot 5" in reason

    async def test_swapping_two_users_pins_recovers_once_both_are_set(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        A swap done in place turns the first slot edited off (as before the swap).

        Re-enabling it once the other slot moved on puts both in sync and
        clears the repair.
        """
        entry = fake_zwave_lock_entry
        await self._settle(hass, freezer, entry, lock_entity)

        await async_set_slot_pin(hass, entry, 1, "1234")  # slot 2's PIN
        await async_advance_sync_ticks(hass, freezer, 5)
        assert _switch(hass, entry, 1) == STATE_OFF
        assert lcm_issue_ids(hass, "slot_disabled")

        await async_set_slot_pin(hass, entry, 2, "9999")
        await async_advance_sync_ticks(hass, freezer, 5)
        await async_switch_slot(hass, entry, 1, SERVICE_TURN_ON)
        await async_advance_sync_ticks(hass, freezer, 5)

        assert fake_zwave_lock.holds(1) == "1234"
        assert fake_zwave_lock.holds(2) == "9999"
        assert not lcm_issue_ids(hass, "slot_disabled")
        assert not lcm_issue_ids(hass, "slot_suspended")

    async def test_moving_a_pin_to_another_user_after_changing_the_first(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Slot 1 gets a new PIN, then slot 2 takes slot 1's old one: no refusal."""
        entry = fake_zwave_lock_entry
        await self._settle(hass, freezer, entry, lock_entity)

        await async_set_slot_pin(hass, entry, 1, "4321")
        await async_set_slot_pin(hass, entry, 2, "9999")
        await async_advance_sync_ticks(hass, freezer, 5)

        assert fake_zwave_lock.holds(1) == "4321"
        assert fake_zwave_lock.holds(2) == "9999"
        assert not lcm_issue_ids(hass, "slot_disabled")


class TestZWaveReload:
    """The lock's integration reloading underneath a loaded entry."""

    async def test_a_keypad_change_after_zwave_js_reloads_is_put_back(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """After a Z-Wave JS reload, the lock's reports still reach the worker."""
        entry = fake_zwave_lock_entry
        await async_advance_sync_ticks(hass, freezer, 5)
        assert fake_zwave_lock.holds(2) == "1234"

        assert await hass.config_entries.async_reload(zwave_integration.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 30)

        fake_zwave_lock.program(2, "4321")
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.holds(2) == "1234"
        assert (
            hass.states.get(
                in_sync_entity_id(hass, entry, 2, lock_entity.entity_id)
            ).state
            == STATE_ON
        )

    async def test_a_pin_change_after_zwave_js_reloads_is_written(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A PIN changed after the lock's integration reloaded reaches the lock."""
        entry = fake_zwave_lock_entry
        await async_advance_sync_ticks(hass, freezer, 5)

        assert await hass.config_entries.async_reload(zwave_integration.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 30)

        await async_set_slot_pin(hass, entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.holds(2) == "4321"
        assert not lcm_issue_ids(hass, "slot_suspended")
        assert not lcm_issue_ids(hass, "lock_offline")


class TestLockLeavesOneOfTwoEntries:
    """A lock two entries share, taken out of one of them."""

    async def _second(self, hass, lock_entity) -> MockConfigEntry:

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            unique_id="r2b_second",
            subentries_data=user_subentries(
                {3: {CONF_NAME: "slot3", CONF_PIN: "5555", CONF_ENABLED: True}}
            ),
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    @pytest.mark.parametrize("dead", [False, True], ids=["alive", "dead"])
    async def test_the_other_entry_keeps_syncing_and_the_codes_go(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        dead: bool,
    ) -> None:

        first = fake_zwave_lock_entry
        second = await self._second(hass, lock_entity)
        await async_advance_sync_ticks(hass, freezer, 10)
        assert fake_zwave_lock.holds(1) == "9999"
        assert fake_zwave_lock.holds(3) == "5555"

        if dead:
            fake_zwave_lock.mark_dead()
            await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            first, options={CONF_LOCKS: [], CONF_SLOTS: ZWAVE_JS_LCM_CONFIG_SLOTS}
        )
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 10)
        if dead:
            await async_advance_clock(hass, freezer, 3 * 60 * 60)
            fake_zwave_lock.mark_alive()
            await hass.async_block_till_done()
            await async_advance_clock(hass, freezer, 60 * 60)

        # The removed entry's codes are cleared; the other entry's stays.
        assert fake_zwave_lock.holds(1) is None
        assert fake_zwave_lock.holds(2) is None
        assert fake_zwave_lock.holds(3) == "5555"
        fake_zwave_lock.program(3, "4321")
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 10)
        assert fake_zwave_lock.holds(3) == "5555"
        assert (
            hass.states.get(
                in_sync_entity_id(hass, second, 3, lock_entity.entity_id)
            ).state
            == STATE_ON
        )
        assert lcm_issue_ids(hass) == []


_BOTH = "User Code lock advertising User Credential"


class TestUserRemoved:
    """A user deleted from the entry: their code leaves the lock."""

    @pytest.fixture(params=["user_code", "user_credential", "both"])
    def lock_schlage_be469_state(
        self, request: pytest.FixtureRequest, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """
        A User Code lock (the BE469), a User Credential one, or one with both.

        The last advertises User Credential CC beside User Code CC but reports
        no User Credential users, so the driver sends User Code (AC:269-288).
        """
        if request.param == "user_credential":
            return user_credential_lock_state(lock_schlage_be469_state)
        if request.param == "both":
            state = copy.deepcopy(lock_schlage_be469_state)
            state["label"] = _BOTH
            state["endpoints"][0]["commandClasses"].append(
                {"id": USER_CREDENTIAL_CC_ID, "name": "User Credential", "version": 1}
            )
            return state
        return lock_schlage_be469_state

    @pytest.fixture
    def fake_zwave_lock_options(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The lock with both reports no User Credential users."""
        return {"max_users": 0} if lock_schlage_be469_state["label"] == _BOTH else {}

    async def test_a_deleted_user_s_code_is_cleared(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        The release of a removed slot clears it on a slot-addressed lock.

        On a User Code lock there is no lock-side user to carry LCM's tag,
        so the release (``adopt_untagged=False``) finds no owner and sends
        nothing: the deleted user's PIN still opens the door, the worker
        reads it back twice and raises ``slot_clear_failed`` -- which
        acknowledging only repeats.
        """

        entry = fake_zwave_lock_entry
        await async_advance_sync_ticks(hass, freezer, 5)
        assert fake_zwave_lock.holds(2) == "1234"
        if fake_zwave_lock.node.label == _BOTH:
            assert not fake_zwave_lock.uses_user_credential_cc

        assert hass.config_entries.async_remove_subentry(
            entry, subentry_id_for_slot(entry, 2)
        )
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert fake_zwave_lock.holds(2) is None
        assert fake_zwave_lock.holds(1) == "9999"
        assert not lcm_issue_ids(hass, "slot_suspended")


class TestRestartMidWrite:
    """The entry going down (a restart, a reload) with a write out to the lock."""

    async def test_a_write_cut_off_by_a_reload_lands_after_it(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """The new PIN reaches the lock once the entry is back; no repair is left."""

        entry = fake_zwave_lock_entry
        await async_advance_sync_ticks(hass, freezer, 5)
        with fake_zwave_lock.hang("set", 2) as fault:
            await hass.services.async_call(
                TEXT_DOMAIN,
                SERVICE_SET_VALUE,
                {ATTR_VALUE: "4321"},
                target={
                    ATTR_ENTITY_ID: slot_entity_id(hass, "text", entry, 2, CONF_PIN)
                },
                blocking=True,
            )
            await fault.async_wait_held()
            reload = hass.async_create_task(
                hass.config_entries.async_reload(entry.entry_id)
            )
            for _ in range(20):
                await asyncio.sleep(0)
        assert await reload
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert fake_zwave_lock.holds(2) == "4321"
        assert (
            hass.states.get(
                in_sync_entity_id(hass, entry, 2, lock_entity.entity_id)
            ).state
            == STATE_ON
        )
        assert lcm_issue_ids(hass) == []
