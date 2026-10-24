"""
Passwords beside PINs on a Virtual lock, through the whole runtime.

Every combination of what a slot holds before and what it should hold
after, for both types, is tried: the domain is small enough to cover whole.
Two properties: each type converges to what it wants on its own, and a
release leaves nothing of Lock Code Manager's in the slot, sent to the
provider once.
"""

import itertools
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.ha.runtime import (
    async_give_up,
    async_resume_slot,
    async_want,
    lock_registry,
)
from custom_components.lock_code_manager.model.credentials import (
    CredentialAddress,
    CredentialType,
    pin_address,
)
from custom_components.lock_code_manager.providers.virtual import VirtualLock
from custom_components.lock_code_manager.sync.slot import Repair, Status

from .conftest import VIRTUAL_LOCK_ENTITY_ID, get_virtual_lock

PIN_1 = pin_address(1)
PASSWORD_1 = CredentialAddress(1, CredentialType.PASSWORD)

CASES = list(
    itertools.product(
        (None, "9999"),  # PIN before
        (None, "old pw"),  # password before
        (None, "1111"),  # PIN wanted
        (None, "new pw"),  # password wanted
    )
)


def _held(lock: VirtualLock, field: str) -> str | None:
    return lock._data.get("1", {}).get(field)


async def _start_from(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    lock: VirtualLock,
    pin: str | None,
    password: str | None,
) -> None:
    """Put the slot's starting values on the lock through the runtime."""
    worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
    assert worker is not None
    async_want(
        hass,
        entry,
        VIRTUAL_LOCK_ENTITY_ID,
        1,
        {CredentialType.PIN: pin, CredentialType.PASSWORD: password},
    )
    await worker.settled()
    assert (_held(lock, "code"), _held(lock, "password")) == (pin, password)


@pytest.mark.parametrize(("pin_before", "pw_before", "pin_want", "pw_want"), CASES)
async def test_each_type_converges_on_its_own(
    hass: HomeAssistant,
    lcm_config_entry,
    pin_before: str | None,
    pw_before: str | None,
    pin_want: str | None,
    pw_want: str | None,
) -> None:
    """What each type wants is what the lock holds, whatever the other does."""
    lock = get_virtual_lock(hass, lcm_config_entry)
    await _start_from(hass, lcm_config_entry, lock, pin_before, pw_before)
    worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
    assert worker is not None

    async_want(
        hass,
        lcm_config_entry,
        VIRTUAL_LOCK_ENTITY_ID,
        1,
        {CredentialType.PIN: pin_want, CredentialType.PASSWORD: pw_want},
    )
    await worker.settled()

    assert (_held(lock, "code"), _held(lock, "password")) == (pin_want, pw_want)
    assert worker.status(PIN_1)[0] is Status.IN_SYNC
    assert worker.status(PASSWORD_1)[0] is Status.IN_SYNC


@pytest.mark.parametrize(("pin_before", "pw_before", "pin_want", "pw_want"), CASES)
async def test_a_release_leaves_nothing_and_is_sent_once(
    hass: HomeAssistant,
    lcm_config_entry,
    pin_before: str | None,
    pw_before: str | None,
    pin_want: str | None,
    pw_want: str | None,
) -> None:
    """Giving the slot up clears every type Lock Code Manager keeps there."""
    lock = get_virtual_lock(hass, lcm_config_entry)
    await _start_from(hass, lcm_config_entry, lock, pin_before, pw_before)
    worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
    assert worker is not None
    async_want(
        hass,
        lcm_config_entry,
        VIRTUAL_LOCK_ENTITY_ID,
        1,
        {CredentialType.PIN: pin_want, CredentialType.PASSWORD: pw_want},
    )
    await worker.settled()

    with patch.object(
        VirtualLock,
        "async_release_managed_slot",
        autospec=True,
        side_effect=VirtualLock.async_release_managed_slot,
    ) as released:
        async_give_up(hass, lcm_config_entry, VIRTUAL_LOCK_ENTITY_ID, 1)
        await worker.settled()

    assert "1" not in lock._data
    assert PIN_1 not in worker.slots
    assert PASSWORD_1 not in worker.slots
    assert released.call_count == 1


async def test_a_password_lock_keeps_its_password_clear_until_one_is_wanted(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Nothing configures a password yet: the address wants nothing, in sync."""
    worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
    assert worker is not None
    await worker.settled()

    assert worker.slots[PASSWORD_1].want is None
    assert worker.status(PASSWORD_1)[0] is Status.IN_SYNC


async def _release_pin_only(
    lock: VirtualLock, slot: int, *, written: bool = False
) -> None:
    """Release a slot as a provider that leaves its password behind would."""
    await lock.async_clear_slot_credential(slot, CredentialType.PIN)


async def test_a_resumed_release_goes_to_the_provider_again(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Acknowledging a held release's repair sends the release again."""
    lock = get_virtual_lock(hass, lcm_config_entry)
    await _start_from(hass, lcm_config_entry, lock, "1111", "pw")
    worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
    assert worker is not None
    release_everything = VirtualLock.async_release_managed_slot

    with patch.object(
        VirtualLock,
        "async_release_managed_slot",
        autospec=True,
        side_effect=_release_pin_only,
    ) as released:
        async_give_up(hass, lcm_config_entry, VIRTUAL_LOCK_ENTITY_ID, 1)
        await worker.settled()

        assert released.call_count == 1
        assert _held(lock, "password") == "pw"
        assert worker.status(PASSWORD_1) == (
            Status.SUSPENDED,
            Repair.CODE_MAY_REMAIN,
        )

        released.side_effect = release_everything
        await async_resume_slot(hass, VIRTUAL_LOCK_ENTITY_ID, 1)
        await worker.settled()

    assert released.call_count == 2
    assert "1" not in lock._data
    assert PIN_1 not in worker.slots
    assert PASSWORD_1 not in worker.slots
