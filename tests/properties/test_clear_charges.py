"""Stateful property test: a clear is charged once, and only when a read contradicts it.

Drives the real BaseLock clear path and LockUsercodeUpdateCoordinator against
a push lock that never confirms a clear, so every clear is judged by the read
the coordinator makes after it. What the read finds is drawn per clear: the
slot empty, holding a code the lock will not reveal, or holding a readable
code.
"""

from __future__ import annotations

import asyncio

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential

from ..common import MockLCMPushLock

PINS = st.text(alphabet="0123456789", min_size=4, max_size=8)
SLOTS = st.integers(min_value=1, max_value=3)
# What the slot holds once the lock has handled the clear.
OUTCOMES = st.one_of(
    st.just(SlotCredential.empty()),
    st.just(SlotCredential.unreadable()),
    PINS.map(SlotCredential.known),
)


class UnconfirmedClearLock(MockLCMPushLock):
    """A push lock that answers every clear without saying whether it applied it."""

    async def async_clear_usercode(
        self, code_slot: int, *, adopt_untagged: bool = True
    ) -> bool:
        """Report a change, push nothing, and ask for the slot to be read back."""
        self.service_calls["clear_usercode"].append((code_slot,))
        self._request_read_back(code_slot)
        return True

    def hold(self, slot: int, credential: SlotCredential) -> None:
        """Make the lock hold ``credential`` at ``slot`` from now on."""
        self.write_only.discard(slot)
        self.codes.pop(slot, None)
        if credential.is_readable:
            self.codes[slot] = str(credential.readable_pin)
        elif credential.is_present:
            self.write_only.add(slot)


class ClearChargeMachine(RuleBasedStateMachine):
    """Clears judged by the read that follows them, interleaved with later reads."""

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._hass_cm = async_test_home_assistant(self.loop)
        self.hass = self.loop.run_until_complete(self._hass_cm.__aenter__())

        self.config_entry = MockConfigEntry(domain=DOMAIN)
        self.config_entry.add_to_hass(self.hass)
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        lock_entity = ent_reg.async_get_or_create(
            "lock", "test", "pbt_clear_lock", config_entry=self.config_entry
        )
        self.lock = UnconfirmedClearLock(self.hass, dev_reg, ent_reg, None, lock_entity)
        self.lock.codes = {}
        self.lock._min_operation_delay = 0
        self.coordinator = LockUsercodeUpdateCoordinator(
            self.hass, self.lock, self.config_entry
        )
        self.lock.coordinator = self.coordinator
        self._run(self.coordinator.async_refresh())

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    @rule(slot=SLOTS, outcome=OUTCOMES)
    def clear_then_read(self, slot: int, outcome: SlotCredential) -> None:
        """A clear is charged exactly when the read after it finds a readable code."""
        address = pin_address(slot)
        self.lock.hold(slot, outcome)
        self._run(self.lock.async_internal_clear_usercode(slot, source="sync"))
        # Lets the coordinator's read after the clear run to completion.
        self._run(self.hass.async_block_till_done())

        assert not self.coordinator.has_pending_write(address)
        assert self.coordinator.take_failed_write(address) is outcome.is_readable
        # The same clear is never charged twice.
        assert self.coordinator.take_failed_write(address) is False

    @rule()
    def read_again(self) -> None:
        """A later read charges no clear that an earlier read already settled."""
        self._run(self.coordinator.async_refresh())
        for slot in range(1, 4):
            assert self.coordinator.take_failed_write(pin_address(slot)) is False

    @invariant()
    def nothing_waits_on_a_read(self) -> None:
        """Every clear is settled by the read that follows it."""
        for slot in range(1, 4):
            assert not self.coordinator.has_pending_write(pin_address(slot))

    def teardown(self) -> None:
        async def _shutdown() -> None:
            await self.coordinator.async_shutdown()
            await self.hass.async_stop(force=True)
            await self._hass_cm.__aexit__(None, None, None)

        self.loop.run_until_complete(_shutdown())
        self.loop.close()
        asyncio.set_event_loop(None)


TestClearChargeMachine = ClearChargeMachine.TestCase
