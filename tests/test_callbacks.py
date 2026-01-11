"""Tests for callback registry exception handling."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.lock_code_manager.callbacks import EntityCallbackRegistry


@pytest.fixture
def registry() -> EntityCallbackRegistry:
    """Create a fresh callback registry."""
    return EntityCallbackRegistry()


@pytest.fixture
def mock_ent_reg() -> MagicMock:
    """Create a mock entity registry."""
    return MagicMock()


@pytest.fixture
def mock_lock() -> MagicMock:
    """Create a mock lock."""
    lock = MagicMock()
    lock.lock.entity_id = "lock.test_lock"
    return lock


def test_invoke_standard_adders_exception_handling(
    registry: EntityCallbackRegistry,
    mock_ent_reg: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that exceptions in standard adder callbacks are logged and don't stop others."""

    def failing_callback(slot_num, ent_reg):
        raise ValueError("Test error")

    call_tracker = MagicMock()

    def successful_callback(slot_num, ent_reg):
        call_tracker(slot_num)

    registry.register_standard_adder(failing_callback)
    registry.register_standard_adder(successful_callback)

    registry.invoke_standard_adders(1, mock_ent_reg)

    # The successful callback should still have been called
    call_tracker.assert_called_once_with(1)
    assert "Error in standard entity callback for slot 1" in caplog.text


def test_invoke_lock_slot_adders_exception_handling(
    registry: EntityCallbackRegistry,
    mock_ent_reg: MagicMock,
    mock_lock: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that exceptions in lock-slot adder callbacks are logged."""

    def failing_callback(lock, slot_num, ent_reg):
        raise ValueError("Test error")

    call_tracker = MagicMock()

    def successful_callback(lock, slot_num, ent_reg):
        call_tracker(lock, slot_num)

    registry.register_lock_slot_adder(failing_callback)
    registry.register_lock_slot_adder(successful_callback)

    registry.invoke_lock_slot_adders(mock_lock, 2, mock_ent_reg)

    call_tracker.assert_called_once_with(mock_lock, 2)
    assert (
        "Error in lock-slot entity callback for lock lock.test_lock slot 2"
        in caplog.text
    )


def test_invoke_keyed_adders_exception_handling(
    registry: EntityCallbackRegistry,
    mock_ent_reg: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that exceptions in keyed adder callbacks are logged."""

    def failing_callback(slot_num, ent_reg):
        raise ValueError("Test error")

    call_tracker = MagicMock()

    def successful_callback(slot_num, ent_reg):
        call_tracker(slot_num)

    registry.register_keyed_adder("test_key", failing_callback)
    registry.register_keyed_adder("test_key", successful_callback)

    registry.invoke_keyed_adders("test_key", 3, mock_ent_reg)

    call_tracker.assert_called_once_with(3)
    assert "Error in optional entity callback for key test_key slot 3" in caplog.text


async def test_invoke_entity_removers_for_slot_exception_handling(
    registry: EntityCallbackRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that exceptions in entity remover callbacks are logged and entity is still removed."""
    failing_remover = AsyncMock(side_effect=ValueError("Test error"))
    successful_remover = AsyncMock()

    registry.register_entity_remover("1|failing", failing_remover)
    registry.register_entity_remover("1|success", successful_remover)

    await registry.invoke_entity_removers_for_slot(1)

    failing_remover.assert_called_once()
    successful_remover.assert_called_once()
    assert "Error removing entity with uid 1|failing" in caplog.text
    # Both should be removed from registry even with error
    assert "1|failing" not in registry.remove_entity
    assert "1|success" not in registry.remove_entity


async def test_invoke_entity_removers_for_key_exception_handling(
    registry: EntityCallbackRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that exceptions in entity remover callbacks for key are logged."""
    failing_remover = AsyncMock(side_effect=ValueError("Test error"))
    successful_remover = AsyncMock()

    registry.register_entity_remover("2|test_key", failing_remover)
    registry.register_entity_remover("2|test_key|lock.test", successful_remover)

    await registry.invoke_entity_removers_for_key(2, "test_key")

    failing_remover.assert_called_once()
    successful_remover.assert_called_once()
    assert "Error removing entity with uid 2|test_key" in caplog.text
    # Both should be removed from registry even with error
    assert "2|test_key" not in registry.remove_entity
    assert "2|test_key|lock.test" not in registry.remove_entity


def test_invoke_lock_added_handlers_exception_handling(
    registry: EntityCallbackRegistry,
    mock_lock: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that exceptions in lock added callbacks are logged."""

    def failing_callback(locks):
        raise ValueError("Test error")

    call_tracker = MagicMock()

    def successful_callback(locks):
        call_tracker(locks)

    registry.register_lock_added_handler(failing_callback)
    registry.register_lock_added_handler(successful_callback)

    locks = [mock_lock]
    registry.invoke_lock_added_handlers(locks)

    call_tracker.assert_called_once_with(locks)
    assert "Error in lock added callback" in caplog.text


def test_invoke_lock_removed_handlers_exception_handling(
    registry: EntityCallbackRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that exceptions in lock removed callbacks are logged."""

    def failing_callback(lock_entity_id):
        raise ValueError("Test error")

    call_tracker = MagicMock()

    def successful_callback(lock_entity_id):
        call_tracker(lock_entity_id)

    registry.register_lock_removed_handler(failing_callback)
    registry.register_lock_removed_handler(successful_callback)

    registry.invoke_lock_removed_handlers("lock.test_lock")

    call_tracker.assert_called_once_with("lock.test_lock")
    assert "Error in lock removed callback for lock.test_lock" in caplog.text
