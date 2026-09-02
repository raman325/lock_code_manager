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
    # calculate_in_sync only reads the attributes set here.
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


@given(pin=PINS, other=LAST_SET, same_as_last_set=st.booleans())
def test_active_verified_empty_credential_is_never_in_sync(
    pin: str, other: str | None, same_as_last_set: bool
) -> None:
    """Active + a verified read that the slot is empty: never in sync.

    The coordinator only marks an empty read verified when no write is
    pending against it, so by the time this branch sees EMPTY the lock has
    had its say and nothing we wrote is still in flight. Trusting our own
    write over that -- for any length of time -- is what let a deleted
    credential report as synchronized (issue #1538). A write that could
    still be lagging its read is a pending write, and the ``is_verified``
    guard above already refuses those.

    ``same_as_last_set`` is an explicit axis because two independent draws
    almost never collide, and the case that matters is exactly the one
    where the configured PIN equals the one last written.
    """
    last_set_pin = pin if same_as_last_set else other
    manager = _manager(verified=True, last_set_pin=last_set_pin)
    state = CredentialSyncState(STATE_ON, pin, None, "", SlotCredential.empty())
    assert manager.calculate_in_sync(state) is False


@given(pin=PINS, last_set_pin=LAST_SET)
def test_active_unreadable_credential_compares_last_set(
    pin: str, last_set_pin: str | None
) -> None:
    """Active + write-only code: in sync iff configured PIN equals last set.

    No read will ever say what a write-only lock holds, so the last PIN
    written is the only thing to compare the configured one against.
    """
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
