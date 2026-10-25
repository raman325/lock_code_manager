"""The provider adapter over a Virtual lock: a PIN and a password per slot."""

from unittest.mock import patch

import pytest

from custom_components.lock_code_manager.ha.adapter import ProviderPort
from custom_components.lock_code_manager.model.credentials import (
    CredentialAddress,
    CredentialType,
    SlotCredential,
    pin_address,
)
from custom_components.lock_code_manager.model.exceptions import LockOperationFailed
from custom_components.lock_code_manager.providers.virtual import VirtualLock
from custom_components.lock_code_manager.sync.slot import Holds, Reading, Seen
from custom_components.lock_code_manager.sync.worker import (
    ErrorKind,
    LockError,
    LockWorker,
)

PIN_1 = pin_address(1)
PASSWORD_1 = CredentialAddress(1, CredentialType.PASSWORD)
PASSWORD_2 = CredentialAddress(2, CredentialType.PASSWORD)


@pytest.fixture
def port(virtual_lock: VirtualLock) -> ProviderPort:
    """A port over the set-up virtual lock, with a worker that never starts."""
    port = ProviderPort(virtual_lock, name_for=lambda slot: f"User {slot}")
    port.worker = LockWorker(port, b"salt", gap=0.0)
    return port


async def test_each_type_reaches_its_own_credential(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """A password write leaves the PIN, and a read answers per address."""
    await port.write(PIN_1, "1234")
    await port.write(PASSWORD_1, "open sesame")

    assert virtual_lock._data["1"] == {
        "code": "1234",
        "password": "open sesame",
        "name": "User 1",
    }
    assert await port.read([PIN_1, PASSWORD_1]) == {
        PIN_1: Holds("1234"),
        PASSWORD_1: Holds("open sesame"),
    }


async def test_clearing_the_password_leaves_the_pin(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """A clear is of one type."""
    await port.write(PIN_1, "1234")
    await port.write(PASSWORD_1, "pw")

    await port.write(PASSWORD_1, None)

    assert await port.read([PIN_1, PASSWORD_1]) == {
        PIN_1: Holds("1234"),
        PASSWORD_1: Seen.EMPTY,
    }


async def test_a_release_goes_with_the_pin_clear(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """The password's clear is its own; the PIN's sends the slot's release."""
    await port.write(PIN_1, "1234")
    await port.write(PASSWORD_1, "pw")
    port.release(1, written=True)

    with patch.object(
        VirtualLock,
        "async_release_managed_slot",
        autospec=True,
        side_effect=VirtualLock.async_release_managed_slot,
    ) as released:
        await port.write(PASSWORD_1, None)
        assert released.call_count == 0
        assert await port.read([PIN_1, PASSWORD_1]) == {
            PIN_1: Holds("1234"),
            PASSWORD_1: Seen.EMPTY,
        }

        await port.write(PIN_1, None)

    assert released.call_count == 1
    assert "1" not in virtual_lock._data


async def test_a_password_write_does_not_hide_a_pin_push(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """The write's own echo is of its address only."""
    assert port.worker is not None
    port.worker.want(PIN_1, "1234")
    pushes: list[tuple[CredentialAddress, Reading]] = []
    real_set = VirtualLock.async_set_credential

    async def _set_and_push(self, *args, **kwargs):
        port.pushed(PIN_1, SlotCredential.known("9999"))
        return await real_set(self, *args, **kwargs)

    with (
        patch.object(VirtualLock, "async_set_credential", _set_and_push),
        patch.object(
            port.worker,
            "push",
            side_effect=lambda address, seen: pushes.append((address, seen)),
        ),
    ):
        await port.write(PASSWORD_1, "pw")

    assert pushes == [(PIN_1, Holds("9999"))]


async def test_duplicates_are_refused_within_a_type_only(port: ProviderPort) -> None:
    """A password may equal a PIN; not another slot's password."""
    assert port.worker is not None
    port.worker.want(pin_address(2), "same")
    await port.write(PASSWORD_1, "same")

    port.worker.want(PASSWORD_2, "taken")
    with pytest.raises(LockError) as refused:
        await port.write(PASSWORD_1, "taken")
    assert (refused.value.kind, refused.value.sent) == (
        ErrorKind.VALUE_REFUSED,
        False,
    )


async def test_one_type_failing_to_read_leaves_the_others(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """A type whose read fails is left out; the read fails only if every type does."""
    await port.write(PIN_1, "1234")
    real_get = VirtualLock.async_get_slot_credentials

    async def _password_fails(self, credential_type, slots=None):
        if credential_type is CredentialType.PASSWORD:
            raise LockOperationFailed("password read failed")
        return await real_get(self, credential_type, slots)

    with patch.object(VirtualLock, "async_get_slot_credentials", _password_fails):
        assert await port.read([PIN_1, PASSWORD_1]) == {PIN_1: Holds("1234")}
        with pytest.raises(LockError) as failed:
            await port.read([PASSWORD_1])
    assert failed.value.kind is ErrorKind.NO_ANSWER


async def test_a_type_that_answered_nothing_is_no_failure(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """One type answers with nothing to say yet, the other fails: still an answer."""
    real_get = VirtualLock.async_get_slot_credentials

    async def _pin_fails(self, credential_type, slots=None):
        if credential_type is CredentialType.PIN:
            raise LockOperationFailed("pin read failed")
        return await real_get(self, credential_type, slots)

    with (
        patch.object(VirtualLock, "read_lag", 60.0),
        patch.object(VirtualLock, "async_get_slot_credentials", _pin_fails),
    ):
        await port.write(PASSWORD_1, "pw")
        assert await port.read([PIN_1, PASSWORD_1]) == {}


async def test_only_a_pin_read_says_the_lock_reports_its_codes(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """The slots a lock cannot tell of are PIN slots: a password read says nothing of them."""
    await port.write(PASSWORD_1, "pw")
    lock = virtual_lock.lock.id

    assert await port.read([PASSWORD_1]) == {PASSWORD_1: Holds("pw")}
    assert not port.reports.get(lock)

    await port.write(PIN_1, "1234")
    await port.read([PIN_1])
    assert port.reports[lock] is True


async def test_a_type_virtual_does_not_store_is_refused_unsent(
    port: ProviderPort,
) -> None:
    """It advertises every type, and writes only those it keeps."""
    rfid = CredentialAddress(1, CredentialType.RFID)

    with pytest.raises(LockError) as refused:
        await port.write(rfid, "card")
    assert (refused.value.kind, refused.value.sent) == (ErrorKind.SLOT_REFUSED, False)


async def test_a_pin_push_reaches_the_pin_address(port: ProviderPort) -> None:
    """What a provider pushes about a slot is about its PIN."""
    assert port.worker is not None
    port.worker.want(PIN_1, "1234")
    pushes: list[tuple[CredentialAddress, Reading]] = []

    with patch.object(
        port.worker,
        "push",
        side_effect=lambda address, seen: pushes.append((address, seen)),
    ):
        port.pushed(PIN_1, SlotCredential.empty())

    assert pushes == [(PIN_1, Seen.EMPTY)]


async def test_a_lagging_read_leaves_out_only_the_type_written(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """A password just written reads as silent; the slot's PIN is still read."""
    await virtual_lock.async_set_usercode(1, "1234")

    with patch.object(VirtualLock, "read_lag", 60.0):
        await port.write(PASSWORD_1, "pw")
        assert await port.read([PIN_1, PASSWORD_1]) == {PIN_1: Holds("1234")}


async def test_a_release_lags_every_type_it_removed(
    port: ProviderPort, virtual_lock: VirtualLock
) -> None:
    """A release sent with the PIN's clear removes the password too, just as lately."""
    await virtual_lock.async_set_slot_credential(
        1, CredentialType.PASSWORD, "pw", name=None, source="sync"
    )
    port.release(1, written=True)

    with patch.object(VirtualLock, "read_lag", 60.0):
        await port.write(PIN_1, None)
        assert await port.read([PASSWORD_1]) == {}
