"""Stateful property test: a clear is charged once, and only when a read contradicts it.

Drives the real BaseLock clear and set paths and LockUsercodeUpdateCoordinator
against a push lock that never confirms a clear, so every clear is judged by
whatever the lock says next: the coordinator's read, or a push that arrives
first. What the lock holds after each clear is drawn: nothing, a code it will
not reveal, or a readable code.

Clears may pile up before anything reads them, a set may supersede a clear
still waiting, and a newer clear supersedes an older one's verdict that no
sync has taken yet. The oracle is the charge each slot owes when the sync tick
next asks: the verdict of the last clear there, or nothing once a set has
replaced it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_test_home_assistant,
)

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONFIRM_READ_INTERVAL,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential

from ..common import MockLCMPushLock, user_subentries

SLOT_NUMBERS = (1, 2, 3)
SLOTS = st.sampled_from(SLOT_NUMBERS)
KINDS = st.sampled_from(["empty", "unreadable", "readable"])
# Prefixed with the slot number when used, so no two slots ever hold the same
# code and the duplicate-code guard never refuses a set.
SUFFIXES = st.text(alphabet="0123456789", min_size=4, max_size=4)


def credential(kind: str, slot: int, suffix: str) -> SlotCredential:
    """Return what a slot holds, for a drawn kind."""
    if kind == "empty":
        return SlotCredential.empty()
    if kind == "unreadable":
        return SlotCredential.unreadable()
    return SlotCredential.known(f"{slot}{suffix}")


class UnconfirmedClearLock(MockLCMPushLock):
    """A push lock that answers every clear without saying whether it applied it."""

    async def async_clear_usercode(
        self, code_slot: int, *, adopt_untagged: bool = True
    ) -> bool:
        """Report a change, push nothing, and leave the slot to be read back."""
        self.service_calls["clear_usercode"].append((code_slot,))
        self._record_unconfirmed_clear(code_slot)
        return True

    def hold(self, slot: int, held: SlotCredential) -> None:
        """Make the lock hold ``held`` at ``slot`` from now on."""
        self.write_only.discard(slot)
        self.codes.pop(slot, None)
        if held.is_readable:
            self.codes[slot] = str(held.readable_pin)
        elif held.is_present:
            self.write_only.add(slot)


class ClearChargeMachine(RuleBasedStateMachine):
    """Clears, sets, pushes and reads in any order, against one charge per clear."""

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._hass_cm = async_test_home_assistant(self.loop)
        self.hass = self.loop.run_until_complete(self._hass_cm.__aenter__())

        lock_entry = MockConfigEntry(domain="test")
        lock_entry.add_to_hass(self.hass)
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        lock_entity = ent_reg.async_get_or_create(
            "lock", "test", "pbt_clear_lock", config_entry=lock_entry
        )
        # The slots are managed: only a managed slot's clear is judged.
        self.config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            subentries_data=user_subentries(
                {
                    slot: {
                        CONF_NAME: f"user {slot}",
                        CONF_PIN: f"{slot}000",
                        CONF_ENABLED: False,
                    }
                    for slot in SLOT_NUMBERS
                }
            ),
        )
        self.config_entry.add_to_hass(self.hass)
        self.lock = UnconfirmedClearLock(self.hass, dev_reg, ent_reg, None, lock_entity)
        self.lock.codes = {}
        self.lock._min_operation_delay = 0
        self.coordinator = LockUsercodeUpdateCoordinator(
            self.hass, self.lock, self.config_entry
        )
        self.lock.coordinator = self.coordinator
        self._run(self.coordinator.async_refresh())
        # The charge the sync tick would take next, per slot.
        self.owed: dict[int, bool] = {}

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _settle(self) -> None:
        """Let every look run, including one a timer is holding back."""
        for _ in range(3):
            async_fire_time_changed(
                self.hass,
                dt_util.utcnow() + timedelta(seconds=CONFIRM_READ_INTERVAL + 1),
            )
            self._run(self.hass.async_block_till_done())
            if not any(
                self.coordinator.has_pending_write(pin_address(slot))
                for slot in SLOT_NUMBERS
            ):
                return
        raise AssertionError("a clear was never settled by a read")

    @rule(slot=SLOTS, kind=KINDS, suffix=SUFFIXES)
    def clear(self, slot: int, kind: str, suffix: str) -> None:
        """Issue a clear and leave it to be read whenever the lock is next asked."""
        held = credential(kind, slot, suffix)
        self.lock.hold(slot, held)
        self._run(self.lock.async_internal_clear_usercode(slot, source="sync"))
        # Whatever reads it, the lock holds ``held`` until something changes it.
        self.owed[slot] = held.is_readable

    @rule(slot=SLOTS, kind=KINDS, suffix=SUFFIXES)
    def push(self, slot: int, kind: str, suffix: str) -> None:
        """The lock reports a slot unasked; it settles a clear still waiting there."""
        observed = credential(kind, slot, suffix)
        waiting = self.coordinator.has_pending_write(pin_address(slot))
        self.lock.hold(slot, observed)
        self.lock._confirm_slot(slot, observed)
        if waiting:
            self.owed[slot] = observed.is_readable

    @rule(slot=SLOTS, suffix=SUFFIXES)
    def set_code(self, slot: int, suffix: str) -> None:
        """A confirmed set supersedes the slot's clear, settled or not."""
        pin = f"{slot}{suffix}"
        changed = self.lock.codes.get(slot) != pin
        self.lock.write_only.discard(slot)
        self._run(self.lock.async_internal_set_usercode(slot, pin, name=None))
        if changed:
            self.owed[slot] = False

    @rule()
    def settle(self) -> None:
        """Every clear is settled by a read, and the verdicts wait to be taken."""
        self._settle()

    @rule()
    def charge(self) -> None:
        """Each slot owes exactly its last clear's verdict, and owes it once."""
        self._settle()
        for slot in SLOT_NUMBERS:
            address = pin_address(slot)
            assert self.coordinator.take_failed_write(address) is self.owed.pop(
                slot, False
            )
            assert self.coordinator.take_failed_write(address) is False

    def teardown(self) -> None:
        async def _shutdown() -> None:
            await self.coordinator.async_shutdown()
            await self.hass.async_stop(force=True)
            await self._hass_cm.__aexit__(None, None, None)

        self.loop.run_until_complete(_shutdown())
        self.loop.close()
        asyncio.set_event_loop(None)


TestClearChargeMachine = ClearChargeMachine.TestCase
