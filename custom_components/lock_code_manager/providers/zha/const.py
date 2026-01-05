"""Constants for ZHA (Zigbee Home Automation) lock provider.

Custom mappings specific to this provider. ZCL types come directly from zigpy.
"""

from __future__ import annotations

from zigpy.zcl.clusters.closures import DoorLock

# Map operation events to locked state (True = locked, False = unlocked)
OPERATION_TO_LOCKED: dict[DoorLock.OperationEvent, bool] = {
    DoorLock.OperationEvent.Lock: True,
    DoorLock.OperationEvent.KeyLock: True,
    DoorLock.OperationEvent.AutoLock: True,
    DoorLock.OperationEvent.Manual_Lock: True,
    DoorLock.OperationEvent.ScheduleLock: True,
    DoorLock.OperationEvent.OnTouchLock: True,
    DoorLock.OperationEvent.Unlock: False,
    DoorLock.OperationEvent.KeyUnlock: False,
    DoorLock.OperationEvent.Manual_Unlock: False,
    DoorLock.OperationEvent.ScheduleUnlock: False,
}

# Map operation source to human-readable name
OPERATION_SOURCE_NAMES: dict[DoorLock.OperationEventSource, str] = {
    DoorLock.OperationEventSource.Keypad: "Keypad",
    DoorLock.OperationEventSource.RF: "RF",
    DoorLock.OperationEventSource.Manual: "Manual",
    DoorLock.OperationEventSource.RFID: "RFID",
    DoorLock.OperationEventSource.Indeterminate: "Unknown",
}
