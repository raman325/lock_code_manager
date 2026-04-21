"""Test the Virtual lock platform."""

from datetime import timedelta

from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.virtual import VirtualLock


async def test_door_lock(virtual_lock: VirtualLock):
    """Test a lock entity."""
    lock = virtual_lock
    assert lock.usercode_scan_interval == timedelta(minutes=1)
    assert lock.domain == "virtual"
    assert await lock.async_internal_is_integration_connected()
    assert lock._data == {}
    await lock.async_internal_hard_refresh_codes()
    assert lock._data == {}
    # clearing a usercode that does not exist is a no-op (returns False, no error)
    await lock.async_internal_clear_usercode(1)
    assert lock._data == {}

    # we should be able to set a usercode and see it in the data
    await lock.async_internal_set_usercode(1, "1111", "test")
    assert lock._data["1"] == {"code": "1111", "name": "test"}
    await lock.async_internal_get_usercodes()
    assert lock._data["1"] == {"code": "1111", "name": "test"}

    # if we unload without removing permanently, the data should be saved
    config_entry = lock._lcm_config_entry
    assert await lock.async_unload(False) is None
    assert await lock.async_setup_internal(config_entry) is None
    assert lock._data["1"] == {"code": "1111", "name": "test"}

    # we can clear a valid usercode
    await lock.async_internal_set_usercode(2, "2222", "test2")
    assert lock._data["2"] == {"code": "2222", "name": "test2"}
    await lock.async_internal_clear_usercode(2)
    assert "2" not in lock._data

    # if we unload with removing permanently, the data should be removed
    assert await lock.async_unload(True) is None
    assert await lock.async_setup_internal(config_entry) is None
    assert not lock._data


async def test_set_usercode_returns_changed_status(virtual_lock: VirtualLock):
    """Test that set_usercode returns True when value changes, False when unchanged."""
    lock = virtual_lock

    # First set should return True (value changed from empty)
    changed = await lock.async_set_usercode(1, "1234", "test")
    assert changed is True
    assert lock._data["1"] == {"code": "1234", "name": "test"}

    # Setting the same value should return False (no change)
    changed = await lock.async_set_usercode(1, "1234", "test")
    assert changed is False

    # Changing the code should return True
    changed = await lock.async_set_usercode(1, "5678", "test")
    assert changed is True
    assert lock._data["1"] == {"code": "5678", "name": "test"}

    # Changing the name should return True
    changed = await lock.async_set_usercode(1, "5678", "new_name")
    assert changed is True
    assert lock._data["1"] == {"code": "5678", "name": "new_name"}


async def test_clear_usercode_returns_changed_status(virtual_lock: VirtualLock):
    """Test that clear_usercode returns True when value changes, False when already cleared."""
    lock = virtual_lock

    # Clearing non-existent slot should return False
    changed = await lock.async_clear_usercode(1)
    assert changed is False

    # Set a usercode first
    await lock.async_set_usercode(1, "1234", "test")
    assert "1" in lock._data

    # Clearing existing slot should return True
    changed = await lock.async_clear_usercode(1)
    assert changed is True
    assert "1" not in lock._data

    # Clearing again should return False (already cleared)
    changed = await lock.async_clear_usercode(1)
    assert changed is False


async def test_virtual_lock_does_not_support_code_slot_events(
    virtual_lock: VirtualLock,
):
    """Test that virtual locks do not support code slot events."""
    assert virtual_lock.supports_code_slot_events is False


async def test_get_usercodes_returns_empty_for_cleared_slots(
    virtual_lock_with_slots: VirtualLock,
):
    """Test that async_get_usercodes returns SlotCode.EMPTY for cleared slots."""
    lock = virtual_lock_with_slots

    # Set code on slot 1 only
    await lock.async_set_usercode(1, "1234", "slot1")

    # Get usercodes: slot 1 should have code, slot 2 should be EMPTY
    codes = await lock.async_get_usercodes()
    assert codes[1] == "1234"
    assert codes[2] is SlotCode.EMPTY

    # Clear slot 1 and verify it becomes EMPTY
    await lock.async_clear_usercode(1)
    codes = await lock.async_get_usercodes()
    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_includes_unmanaged_slots(
    virtual_lock_with_slots: VirtualLock,
):
    """Test that async_get_usercodes returns unmanaged occupied slots."""
    lock = virtual_lock_with_slots

    # Set code on managed slot 1 and unmanaged slot 99
    await lock.async_set_usercode(1, "1234", "slot1")
    lock._data["99"] = {"code": "9999", "name": "unmanaged"}

    codes = await lock.async_get_usercodes()
    assert codes[1] == "1234"
    assert codes[2] is SlotCode.EMPTY
    assert codes[99] == "9999"


async def test_get_usercodes_invalid_stored_key_skipped(
    virtual_lock_with_slots: VirtualLock,
):
    """Test that invalid stored slot keys are skipped with a warning."""
    lock = virtual_lock_with_slots

    lock._data["bad_key"] = {"code": "1111", "name": "corrupt"}
    lock._data["3"] = {"code": "3333", "name": "valid_unmanaged"}

    codes = await lock.async_get_usercodes()
    # "bad_key" should be skipped; slot 3 should appear
    assert 3 in codes
    assert codes[3] == "3333"
