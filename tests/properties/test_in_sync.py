"""Property-based tests for SlotSyncManager.calculate_in_sync."""

from __future__ import annotations

from types import SimpleNamespace

from hypothesis import given, strategies as st

from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.domain.sync import (
    CredentialSyncState,
    SlotSyncManager,
)

PINS = st.text(alphabet="0123456789", min_size=4, max_size=8)
LAST_SET = st.one_of(st.none(), PINS)
CREDENTIALS = st.one_of(
    st.none(),
    st.just(SlotCredential.empty()),
    st.just(SlotCredential.unreadable()),
    PINS.map(SlotCredential.known),
)
SLOT_STATES = st.builds(
    CredentialSyncState,
    active_state=st.sampled_from([STATE_ON, STATE_OFF]),
    credential_state=PINS,
    name_state=st.one_of(st.none(), st.text(max_size=20)),
    code_state=st.one_of(st.just(""), PINS),
    coordinator_credential=CREDENTIALS,
)


def _manager(
    *, verified: bool, last_set_pin: str | None, cleared_slot: bool = False
) -> SlotSyncManager:
    # __new__ skips the heavyweight __init__ (hass, registries, entities);
    # calculate_in_sync only touches these four attributes.
    manager = SlotSyncManager.__new__(SlotSyncManager)
    manager._slot_num = 1
    manager._address = pin_address(1)
    manager._coordinator = SimpleNamespace(is_verified=lambda address: verified)
    manager._last_set_pin = last_set_pin
    manager._lock = SimpleNamespace(last_write_was_clear=lambda slot: cleared_slot)
    return manager


@given(snapshot=SLOT_STATES, last_set_pin=LAST_SET)
def test_unverified_slot_is_never_in_sync(
    snapshot: CredentialSyncState, last_set_pin: str | None
) -> None:
    """An optimistic write awaiting confirmation can never read as in sync."""
    manager = _manager(verified=False, last_set_pin=last_set_pin)
    assert manager.calculate_in_sync(snapshot) is False


@given(pin=PINS, credential_pin=PINS, last_set_pin=LAST_SET)
def test_active_readable_credential_syncs_iff_pin_matches(
    pin: str, credential_pin: str, last_set_pin: str | None
) -> None:
    """Active slot with a readable code: in sync exactly when PINs match."""
    manager = _manager(verified=True, last_set_pin=last_set_pin)
    state = CredentialSyncState(
        STATE_ON, pin, None, "", SlotCredential.known(credential_pin)
    )
    assert manager.calculate_in_sync(state) is (pin == credential_pin)


@given(pin=PINS, last_set_pin=LAST_SET)
def test_active_empty_credential_trusts_recent_set_only(
    pin: str, last_set_pin: str | None
) -> None:
    """Active + lock reports empty: in sync only if we just set this exact PIN."""
    manager = _manager(verified=True, last_set_pin=last_set_pin)
    state = CredentialSyncState(STATE_ON, pin, None, "", SlotCredential.empty())
    assert manager.calculate_in_sync(state) is (
        last_set_pin is not None and pin == last_set_pin
    )


@given(pin=PINS, last_set_pin=LAST_SET)
def test_active_unreadable_credential_compares_last_set(
    pin: str, last_set_pin: str | None
) -> None:
    """Active + write-only code: in sync iff configured PIN equals last set."""
    manager = _manager(verified=True, last_set_pin=last_set_pin)
    state = CredentialSyncState(STATE_ON, pin, None, "", SlotCredential.unreadable())
    assert manager.calculate_in_sync(state) is (pin == last_set_pin)


@given(pin=PINS, code=st.one_of(st.just(""), PINS), last_set_pin=LAST_SET)
def test_active_without_coordinator_data_falls_back_to_code_sensor(
    pin: str, code: str, last_set_pin: str | None
) -> None:
    """No coordinator data: the code sensor entity is the comparison source."""
    manager = _manager(verified=True, last_set_pin=last_set_pin)
    state = CredentialSyncState(STATE_ON, pin, None, code, None)
    assert manager.calculate_in_sync(state) is (pin == code)


@given(snapshot=SLOT_STATES, last_set_pin=LAST_SET, cleared_slot=st.booleans())
def test_inactive_slot_syncs_iff_the_lock_shows_no_code_it_can_report(
    snapshot: CredentialSyncState, last_set_pin: str | None, cleared_slot: bool
) -> None:
    """Inactive slot: in sync exactly when no code the lock can report remains.

    A lock that reports the slot occupied but withholds its contents can
    neither confirm nor deny a clear, so the clear already issued is the only
    evidence there is. Everything else is unchanged: a readable code means
    work to do whatever was cleared before, and an empty slot is done.
    """
    manager = _manager(
        verified=True, last_set_pin=last_set_pin, cleared_slot=cleared_slot
    )
    state = CredentialSyncState(
        STATE_OFF,
        snapshot.credential_state,
        snapshot.name_state,
        snapshot.code_state,
        snapshot.coordinator_credential,
    )
    credential = state.coordinator_credential
    if credential is None:
        expected = state.code_state == ""
    elif credential.is_empty:
        expected = True
    elif credential.is_readable:
        expected = False
    else:
        expected = cleared_slot
    assert manager.calculate_in_sync(state) is expected
