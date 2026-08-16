"""Stateful property test for credential write orchestration.

Drives the real BaseLock write path (rate limiting, duplicate detection,
connection checks, WriteResult handling) and LockUsercodeUpdateCoordinator
against MockLCMLock, comparing everything to a plain-dict oracle.
"""

from __future__ import annotations

import asyncio

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.domain.exceptions import (
    DuplicateCodeError,
    LockDisconnected,
)

from ..common import MockLCMLock

PINS = st.text(alphabet="0123456789", min_size=4, max_size=8)
SLOTS = st.integers(min_value=1, max_value=5)


class CredentialMachine(RuleBasedStateMachine):
    """Random interleavings of writes, deletes, faults, and refreshes."""

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
            "lock", "test", "pbt_lock", config_entry=self.config_entry
        )
        self.lock = MockLCMLock(self.hass, dev_reg, ent_reg, None, lock_entity)
        self.lock.codes = {}
        # Rate-limit delay between operations would dominate machine runtime.
        self.lock._min_operation_delay = 0
        # Machine correctness also assumes an example never spans the
        # coordinator's 10-second request-refresh cooldown: past it, the
        # deferred debounced refresh could freshen coordinator.data between a
        # rule's _would_duplicate read and its assertion. Examples run in
        # milliseconds, leaving orders of magnitude of margin.
        self.coordinator = LockUsercodeUpdateCoordinator(
            self.hass, self.lock, self.config_entry
        )
        self.lock.coordinator = self.coordinator
        self._run(self.coordinator.async_refresh())

        # Oracle: lock-truth we expect, slot -> pin.
        self.expected: dict[int, str] = {}

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _would_duplicate(self, slot: int, pin: str) -> bool:
        # Mirror of the source _check_duplicate_code reads: coordinator.data,
        # which may lag lock.codes until the next refresh.
        return any(
            other_slot != slot and credential.matches(pin)
            for other_slot, credential in self.coordinator.credentials_by_slot().items()
        )

    @rule(slot=SLOTS, pin=PINS)
    def set_credential(self, slot: int, pin: str) -> None:
        if self._would_duplicate(slot, pin):
            before = dict(self.lock.codes)
            with pytest.raises(DuplicateCodeError):
                self._run(
                    self.lock.async_internal_set_usercode(
                        slot, pin, name=f"PBT user {slot}"
                    )
                )
            assert self.lock.codes == before
        else:
            already_set = self.lock.codes.get(slot) == pin
            self._run(
                self.lock.async_internal_set_usercode(
                    slot, pin, name=f"PBT user {slot}"
                )
            )
            self.expected[slot] = pin
            if not already_set:
                # Names must reach the provider verbatim (tagging is a
                # name-keyed-provider concern, not BaseLock's).
                assert self.lock.service_calls["set_usercode"][-1] == (
                    slot,
                    pin,
                    f"PBT user {slot}",
                )

    @rule(slot=SLOTS)
    def delete_credential(self, slot: int) -> None:
        self._run(self.lock.async_internal_clear_usercode(slot))
        self.expected.pop(slot, None)

    @rule(slot=SLOTS)
    def set_same_pin_is_no_change(self, slot: int) -> None:
        if slot not in self.expected:
            return
        pin = self.expected[slot]
        if self._would_duplicate(slot, pin):
            # An external change may have copied this PIN onto another slot;
            # the duplicate guard fires before the no-change shortcut.
            return
        calls_before = len(self.lock.service_calls["set_usercode"])
        self._run(
            self.lock.async_internal_set_usercode(slot, pin, name=f"PBT user {slot}")
        )
        assert len(self.lock.service_calls["set_usercode"]) == calls_before

    @rule(slot=SLOTS, pin=PINS)
    def external_change(self, slot: int, pin: str) -> None:
        self.lock.codes[slot] = pin
        self.expected[slot] = pin

    @rule()
    def refresh_converges_coordinator(self) -> None:
        self._run(self.coordinator.async_refresh())
        observed = {
            slot: credential.readable_pin
            for slot, credential in self.coordinator.credentials_by_slot().items()
            if credential.is_present
        }
        assert observed == self.expected

    @rule(slot=SLOTS, pin=PINS)
    def write_while_disconnected_fails_loud(self, slot: int, pin: str) -> None:
        self.lock.set_connected(False)
        before = dict(self.lock.codes)
        try:
            with pytest.raises(LockDisconnected):
                self._run(
                    self.lock.async_internal_set_usercode(
                        slot, pin, name=f"PBT user {slot}"
                    )
                )
            assert self.lock.codes == before
        finally:
            self.lock.set_connected(True)

    @invariant()
    def lock_state_matches_oracle(self) -> None:
        assert self.lock.codes == self.expected

    def teardown(self) -> None:
        async def _shutdown() -> None:
            await self.coordinator.async_shutdown()
            await self.hass.async_stop(force=True)
            await self._hass_cm.__aexit__(None, None, None)

        self.loop.run_until_complete(_shutdown())
        self.loop.close()
        asyncio.set_event_loop(None)


TestCredentialMachine = CredentialMachine.TestCase
