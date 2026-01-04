# Adding a New Lock Provider

This tutorial walks through adding support for a new lock integration to Lock Code Manager.
We'll create a provider for a hypothetical "SmartLock" integration.

## Prerequisites

Before starting, ensure:

1. The lock integration exists in Home Assistant and creates `lock.*` entities
2. The integration provides a way to read and write usercodes
3. You understand the integration's data model and API

## Step 1: Create the Provider File

Create a new file in `custom_components/lock_code_manager/providers/`:

```python
# custom_components/lock_code_manager/providers/smartlock.py
"""Module for SmartLock integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback

from ..exceptions import LockDisconnected
from ._base import BaseLock

_LOGGER = logging.getLogger(__name__)


@dataclass(repr=False, eq=False)
class SmartLockLock(BaseLock):
    """Class to represent SmartLock lock."""

    # Add any provider-specific fields here
    _cache: dict[int, str] = field(default_factory=dict, init=False)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "smartlock"
```

## Step 2: Implement Required Properties

### domain

Return the Home Assistant integration domain:

```python
@property
def domain(self) -> str:
    """Return integration domain."""
    return "smartlock"
```

### Update Intervals (Optional)

Override the default intervals if needed:

```python
@property
def usercode_scan_interval(self) -> timedelta:
    """Return scan interval for usercodes."""
    return timedelta(minutes=1)  # Default is 1 minute

@property
def hard_refresh_interval(self) -> timedelta | None:
    """Return interval between hard refreshes."""
    return timedelta(hours=1)  # Or None to disable

@property
def connection_check_interval(self) -> timedelta | None:
    """Return interval for connection state checks."""
    return timedelta(seconds=30)  # Default, or None to disable
```

## Step 3: Implement Required Methods

### is_connection_up()

Check if the lock is reachable:

```python
def is_connection_up(self) -> bool:
    """Return whether connection to lock is up."""
    # Option 1: Check config entry state
    if self.lock_config_entry:
        return self.lock_config_entry.state == ConfigEntryState.LOADED

    # Option 2: Check device-specific connection
    # return self._get_device().is_connected()

    # Option 3: Check entity state
    state = self.hass.states.get(self.lock.entity_id)
    return state is not None and state.state not in ("unavailable", "unknown")
```

### get_usercodes()

Return current usercodes from the lock:

```python
def get_usercodes(self) -> dict[int, int | str]:
    """Get dictionary of code slots and usercodes.

    Returns:
        Dict mapping slot number to usercode.
        Empty string "" for cleared/unused slots.

    Raises:
        LockDisconnected: If lock cannot be communicated with.
    """
    try:
        # Get usercodes from your integration
        # This might be from a cache, coordinator, or direct API call
        device = self._get_device()
        codes = device.get_all_codes()

        return {
            slot: code if code else ""
            for slot, code in codes.items()
        }
    except SomeDeviceError as err:
        raise LockDisconnected(f"Cannot get codes: {err}") from err
```

### set_usercode()

Set a usercode on a slot:

```python
def set_usercode(
    self, code_slot: int, usercode: int | str, name: str | None = None
) -> bool:
    """Set a usercode on a code slot.

    Returns:
        True if value changed, False if already set to this value.

    Raises:
        LockDisconnected: If lock cannot be communicated with.
    """
    try:
        device = self._get_device()

        # Optional: Check if already set to avoid unnecessary writes
        current = device.get_code(code_slot)
        if current == str(usercode):
            return False

        # Set the code
        device.set_code(code_slot, str(usercode), name=name)
        return True

    except SomeDeviceError as err:
        raise LockDisconnected(f"Cannot set code: {err}") from err
```

### clear_usercode()

Clear a usercode from a slot:

```python
def clear_usercode(self, code_slot: int) -> bool:
    """Clear a usercode from a code slot.

    Returns:
        True if value changed, False if already cleared.

    Raises:
        LockDisconnected: If lock cannot be communicated with.
    """
    try:
        device = self._get_device()

        # Optional: Check if already cleared
        current = device.get_code(code_slot)
        if not current:
            return False

        device.clear_code(code_slot)
        return True

    except SomeDeviceError as err:
        raise LockDisconnected(f"Cannot clear code: {err}") from err
```

## Step 4: Implement Optional Methods

### hard_refresh_codes() (Recommended)

If your integration caches data, implement hard refresh:

```python
def hard_refresh_codes(self) -> dict[int, int | str]:
    """Force refresh from device and return all codes."""
    try:
        device = self._get_device()

        # Bypass cache and fetch directly from device
        device.refresh_codes()

        return self.get_usercodes()

    except SomeDeviceError as err:
        raise LockDisconnected(f"Cannot refresh codes: {err}") from err
```

### Setup and Unload

Override if you need custom initialization or cleanup:

```python
async def async_setup(self, config_entry: ConfigEntry) -> None:
    """Set up lock."""
    # Do any provider-specific setup
    self._init_device_connection()

    # Always call super() to set up coordinator
    await super().async_setup(config_entry)

async def async_unload(self, remove_permanently: bool) -> None:
    """Unload lock."""
    # Always call super() first
    await super().async_unload(remove_permanently)

    # Do any provider-specific cleanup
    if remove_permanently:
        self._cleanup_device_data()
```

## Step 5: Add Push Support (Optional)

If your integration supports real-time events:

### Enable Push Mode

```python
@property
def supports_push(self) -> bool:
    """Return whether this lock supports push-based updates."""
    return True

@property
def connection_check_interval(self) -> timedelta | None:
    """Disable connection polling if integration provides state changes."""
    return None
```

### Subscribe to Events

```python
_event_unsub: Callable[[], None] | None = field(init=False, default=None)

@callback
def subscribe_push_updates(self) -> None:
    """Subscribe to real-time value updates."""
    # Idempotent - skip if already subscribed
    if self._event_unsub is not None:
        return

    @callback
    def on_code_changed(event_data: dict[str, Any]) -> None:
        """Handle code change events."""
        slot = event_data["slot"]
        code = event_data.get("code", "")

        # Push to coordinator
        if self.coordinator:
            self.coordinator.push_update({slot: code})

    # Subscribe to your integration's events
    self._event_unsub = self._device.subscribe_code_events(on_code_changed)

@callback
def unsubscribe_push_updates(self) -> None:
    """Unsubscribe from value updates."""
    if self._event_unsub:
        self._event_unsub()
        self._event_unsub = None
```

## Step 6: Register the Provider

Add your provider to the registry in `providers/__init__.py`:

```python
# custom_components/lock_code_manager/providers/__init__.py
"""Integrations module."""

from __future__ import annotations

from ._base import BaseLock
from .smartlock import SmartLockLock  # Add import
from .virtual import VirtualLock
from .zwave_js import ZWaveJSLock

INTEGRATIONS_CLASS_MAP: dict[str, type[BaseLock]] = {
    "smartlock": SmartLockLock,  # Add mapping
    "virtual": VirtualLock,
    "zwave_js": ZWaveJSLock,
}
```

## Step 7: Handle Lock Events (Optional)

If your lock reports when codes are used (e.g., keypad unlock), fire events:

```python
async def async_setup(self, config_entry: ConfigEntry) -> None:
    """Set up lock."""
    await super().async_setup(config_entry)

    # Subscribe to lock/unlock events
    self._listeners.append(
        self.hass.bus.async_listen(
            "smartlock_event",
            self._handle_lock_event,
            self._event_filter,
        )
    )

@callback
def _event_filter(self, event_data: dict[str, Any]) -> bool:
    """Filter events for this lock."""
    return event_data.get("device_id") == self.lock.device_id

@callback
def _handle_lock_event(self, event: Event) -> None:
    """Handle lock/unlock events."""
    code_slot = event.data.get("code_slot")
    is_locked = event.data.get("locked")

    # Fire LCM event for automations
    self.async_fire_code_slot_event(
        code_slot=code_slot,
        to_locked=is_locked,
        action_text=event.data.get("action"),
        source_data=event,
    )
```

## Complete Example

Here's a complete minimal provider:

```python
"""Module for SmartLock integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState

from ..exceptions import LockDisconnected
from ._base import BaseLock


@dataclass(repr=False, eq=False)
class SmartLockLock(BaseLock):
    """Class to represent SmartLock lock."""

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "smartlock"

    @property
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes."""
        return timedelta(minutes=1)

    def is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        if self.lock_config_entry:
            return self.lock_config_entry.state == ConfigEntryState.LOADED
        return True

    def get_usercodes(self) -> dict[int, int | str]:
        """Get dictionary of code slots and usercodes."""
        try:
            # Replace with your integration's API
            codes = self._get_codes_from_device()
            return {slot: code or "" for slot, code in codes.items()}
        except Exception as err:
            raise LockDisconnected(str(err)) from err

    def set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> bool:
        """Set a usercode on a code slot."""
        try:
            self._set_code_on_device(code_slot, str(usercode))
            return True
        except Exception as err:
            raise LockDisconnected(str(err)) from err

    def clear_usercode(self, code_slot: int) -> bool:
        """Clear a usercode from a code slot."""
        try:
            self._clear_code_on_device(code_slot)
            return True
        except Exception as err:
            raise LockDisconnected(str(err)) from err

    def _get_codes_from_device(self) -> dict[int, str]:
        """Get codes from the device (implement for your integration)."""
        # TODO: Implement for your integration
        raise NotImplementedError()

    def _set_code_on_device(self, slot: int, code: str) -> None:
        """Set code on device (implement for your integration)."""
        # TODO: Implement for your integration
        raise NotImplementedError()

    def _clear_code_on_device(self, slot: int) -> None:
        """Clear code on device (implement for your integration)."""
        # TODO: Implement for your integration
        raise NotImplementedError()
```

## Testing Your Provider

### Manual Testing

1. Add your provider to the registry
2. Restart Home Assistant
3. Configure Lock Code Manager with a lock using your integration
4. Verify:
   - Codes can be set and cleared
   - Code sensors show correct values
   - Sync status updates correctly
   - Events fire when codes are used (if applicable)

### Automated Testing

Create tests in `tests/test_smartlock.py`:

```python
"""Tests for SmartLock provider."""

import pytest
from unittest.mock import Mock, patch

from custom_components.lock_code_manager.providers.smartlock import SmartLockLock


@pytest.fixture
def mock_lock():
    """Create a mock SmartLock provider."""
    # Set up mocks for your integration
    ...


async def test_get_usercodes(hass, mock_lock):
    """Test getting usercodes."""
    codes = mock_lock.get_usercodes()
    assert isinstance(codes, dict)
    assert all(isinstance(k, int) for k in codes.keys())


async def test_set_usercode(hass, mock_lock):
    """Test setting a usercode."""
    result = mock_lock.set_usercode(1, "1234")
    assert result is True


async def test_clear_usercode(hass, mock_lock):
    """Test clearing a usercode."""
    result = mock_lock.clear_usercode(1)
    assert result is True
```

## Troubleshooting

### Common Issues

**"Entity not found" errors:**

- Ensure the lock entity exists and is in the entity registry
- Check that `domain` returns the correct integration domain

**Codes not syncing:**

- Verify `get_usercodes()` returns the correct format
- Check that `set_usercode()` actually writes to the device
- Enable debug logging: `logger.setLevel(logging.DEBUG)`

**Push updates not working:**

- Ensure `supports_push` returns `True`
- Verify `subscribe_push_updates()` is being called
- Check that `coordinator.push_update()` is called with correct data

**Connection state issues:**

- Verify `is_connection_up()` accurately reflects device state
- Check that `lock_config_entry` is set correctly

### Debug Logging

Add debug logging to your provider:

```python
import logging

_LOGGER = logging.getLogger(__name__)

def get_usercodes(self) -> dict[int, int | str]:
    _LOGGER.debug("Getting usercodes for %s", self.lock.entity_id)
    codes = self._fetch_codes()
    _LOGGER.debug("Got codes: %s", {k: "****" for k in codes})
    return codes
```

Enable in `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.lock_code_manager.providers.smartlock: debug
```

## Next Steps

- Review existing providers (`zwave_js.py`, `virtual.py`) for examples
- Read [Provider State Management](provider-state-management.md) for advanced topics
- Submit a PR to add your provider to the main repository
