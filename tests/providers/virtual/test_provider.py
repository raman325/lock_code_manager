"""Test the Virtual lock platform."""

from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    User,
    credential_from_slot,
    user_from_slot,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.virtual import VirtualLock


async def test_set_credential_returns_changed_status(virtual_lock: VirtualLock):
    """Test that async_set_credential returns True when value changes, False when unchanged."""
    lock = virtual_lock

    def _make_credential(slot: int, pin: str) -> Credential:
        return credential_from_slot(slot, SlotCredential.known(pin))

    # First set should return True (value changed from empty)
    changed = await lock.async_set_credential(
        1,
        _make_credential(1, "1234"),
        (_make_credential(1, "1234")).readable_pin or "",
        name="test",
        source="direct",
    )
    assert changed is True
    assert lock._data["1"] == {"code": "1234", "name": "test"}

    # Setting the same value should return False (no change)
    changed = await lock.async_set_credential(
        1,
        _make_credential(1, "1234"),
        (_make_credential(1, "1234")).readable_pin or "",
        name="test",
        source="direct",
    )
    assert changed is False

    # Changing the code should return True
    changed = await lock.async_set_credential(
        1,
        _make_credential(1, "5678"),
        (_make_credential(1, "5678")).readable_pin or "",
        name="test",
        source="direct",
    )
    assert changed is True
    assert lock._data["1"] == {"code": "5678", "name": "test"}

    # Changing the name should return True
    changed = await lock.async_set_credential(
        1,
        _make_credential(1, "5678"),
        (_make_credential(1, "5678")).readable_pin or "",
        name="new_name",
        source="direct",
    )
    assert changed is True
    assert lock._data["1"] == {"code": "5678", "name": "new_name"}


async def test_delete_credential_returns_changed_status(virtual_lock: VirtualLock):
    """Test that async_delete_credential returns True when value changes, False when already cleared."""
    lock = virtual_lock

    def _make_ref(slot: int) -> CredentialRef:
        return CredentialRef(user_id=slot, type=CredentialType.PIN, slot=slot)

    # Clearing non-existent slot should return False
    changed = await lock.async_delete_credential(_make_ref(1))
    assert changed is False

    # Set a credential first via the primitive
    await lock.async_set_credential(
        1,
        credential_from_slot(1, SlotCredential.known("1234")),
        (credential_from_slot(1, SlotCredential.known("1234"))).readable_pin or "",
        name="test",
        source="direct",
    )
    assert "1" in lock._data

    # Clearing existing slot should return True
    changed = await lock.async_delete_credential(_make_ref(1))
    assert changed is True
    assert "1" not in lock._data

    # Clearing again should return False (already cleared)
    changed = await lock.async_delete_credential(_make_ref(1))
    assert changed is False


async def test_virtual_lock_does_not_support_code_slot_events(
    virtual_lock: VirtualLock,
):
    """Test that virtual locks do not support code slot events."""
    assert virtual_lock.supports_code_slot_events is False


async def test_get_users_returns_empty_for_cleared_slots(
    virtual_lock_with_slots: VirtualLock,
):
    """Test that async_get_users returns SlotCredential.empty() users for cleared slots."""
    lock = virtual_lock_with_slots

    # Set code on slot 1 only
    await lock.async_set_credential(
        1,
        credential_from_slot(1, SlotCredential.known("1234")),
        (credential_from_slot(1, SlotCredential.known("1234"))).readable_pin or "",
        name="slot1",
        source="direct",
    )

    # async_get_users: slot 1 should have a known credential, slot 2 should be empty
    users = await lock.async_get_users()
    user_map = {u.user_id: u for u in users}
    assert user_map[1].pin_credentials[0].state == SlotCredential.known("1234")
    assert user_map[2].pin_credentials[0].state is SlotCredential.empty()

    # Verify get_usercodes projection (base default projection)
    codes = await lock.async_get_usercodes()
    assert codes[1] == SlotCredential.known("1234")
    assert codes[2] is SlotCredential.empty()

    # Clear slot 1 and verify it becomes EMPTY
    await lock.async_delete_credential(
        CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
    )
    codes = await lock.async_get_usercodes()
    assert codes[1] is SlotCredential.empty()
    assert codes[2] is SlotCredential.empty()


async def test_get_users_includes_unmanaged_occupied_slots(
    virtual_lock_with_slots: VirtualLock,
):
    """Test that async_get_users returns unmanaged occupied slots as users."""
    lock = virtual_lock_with_slots

    # Set code on managed slot 1 and inject an unmanaged slot 99 directly
    await lock.async_set_credential(
        1,
        credential_from_slot(1, SlotCredential.known("1234")),
        (credential_from_slot(1, SlotCredential.known("1234"))).readable_pin or "",
        name="slot1",
        source="direct",
    )
    lock._data["99"] = {"code": "9999", "name": "unmanaged"}

    users = await lock.async_get_users()
    user_map = {u.user_id: u for u in users}

    assert user_map[1].pin_credentials[0].state == SlotCredential.known("1234")
    assert user_map[2].pin_credentials[0].state is SlotCredential.empty()
    assert user_map[99].pin_credentials[0].state == SlotCredential.known("9999")

    # Also verify via the base projection
    codes = await lock.async_get_usercodes()
    assert codes[1] == SlotCredential.known("1234")
    assert codes[2] is SlotCredential.empty()
    assert codes[99] == SlotCredential.known("9999")


async def test_get_users_invalid_stored_key_skipped(
    virtual_lock_with_slots: VirtualLock,
):
    """Test that invalid stored slot keys are skipped with a warning."""
    lock = virtual_lock_with_slots

    lock._data["bad_key"] = {"code": "1111", "name": "corrupt"}
    lock._data["3"] = {"code": "3333", "name": "valid_unmanaged"}

    users = await lock.async_get_users()
    user_map = {u.user_id: u for u in users}

    # "bad_key" should be skipped; slot 3 should appear
    assert 3 in user_map
    assert user_map[3].pin_credentials[0].state == SlotCredential.known("3333")

    # Also verify via the base projection
    codes = await lock.async_get_usercodes()
    assert 3 in codes
    assert codes[3] == SlotCredential.known("3333")


async def test_base_orchestration_set_clear_get(virtual_lock_with_slots: VirtualLock):
    """Test set/clear/get flow through the base primitives + projection."""
    lock = virtual_lock_with_slots

    # async_set_credential routes through the primitive and stores the code
    await lock.async_set_credential(
        1,
        credential_from_slot(1, SlotCredential.known("4321")),
        (credential_from_slot(1, SlotCredential.known("4321"))).readable_pin or "",
        name="orchestration_test",
        source="direct",
    )
    # async_get_usercodes uses the base projection over async_get_users
    codes = await lock.async_get_usercodes()
    assert codes[1] == SlotCredential.known("4321")
    assert codes[2] is SlotCredential.empty()

    # async_delete_credential removes the code
    await lock.async_delete_credential(
        CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
    )
    codes = await lock.async_get_usercodes()
    assert codes[1] is SlotCredential.empty()


async def test_get_users_returns_user_objects(virtual_lock_with_slots: VirtualLock):
    """Test that async_get_users returns properly constructed User objects."""
    lock = virtual_lock_with_slots

    await lock.async_set_credential(
        1,
        credential_from_slot(1, SlotCredential.known("7777")),
        (credential_from_slot(1, SlotCredential.known("7777"))).readable_pin or "",
        name=None,
        source="direct",
    )

    users = await lock.async_get_users()
    assert all(isinstance(u, User) for u in users)

    user_1 = next(u for u in users if u.user_id == 1)
    assert user_1.active is True
    assert len(user_1.pin_credentials) == 1
    assert user_1.pin_credentials[0].slot == 1
    assert user_1.pin_credentials[0].state == SlotCredential.known("7777")

    user_2 = next(u for u in users if u.user_id == 2)
    assert user_2.active is False
    assert user_2.pin_credentials[0].state is SlotCredential.empty()

    # user_from_slot helper produces the same structure
    expected = user_from_slot(1, SlotCredential.known("7777"))
    assert user_1.user_id == expected.user_id
    assert user_1.active == expected.active
    assert user_1.pin_credentials[0].state == expected.pin_credentials[0].state
