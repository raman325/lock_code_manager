"""
The Access Control notifications both Z-Wave providers classify, and the verdicts.

Z-Wave JS and Z-Wave JS UI hear the same driver notification by different
roads -- a Home Assistant bus event keyed by event number, an MQTT topic
named by the event's label -- and must reach the same verdict on it. Each
provider's tests run these cases, so a rule that changes for one and not
the other fails.

References, read at node-zwave-js ``a0cc3b1c0`` (15.31.1+26):
``NOTIF`` = packages/core/src/registries/Notifications.ts,
``NCC`` = packages/cc/src/cc/NotificationCC.ts,
``UCC`` = packages/cc/src/cc/UserCodeCC.ts.
"""

from typing import Any

import pytest
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent as Event,
)

# The driver's labels for the Access Control events (NOTIF:651-846).
ACCESS_CONTROL_EVENT_LABELS: dict[int, str] = {
    Event.MANUAL_LOCK_OPERATION: "Manual lock operation",
    Event.MANUAL_UNLOCK_OPERATION: "Manual unlock operation",
    Event.RF_LOCK_OPERATION: "RF lock operation",
    Event.RF_UNLOCK_OPERATION: "RF unlock operation",
    Event.KEYPAD_LOCK_OPERATION: "Keypad lock operation",
    Event.KEYPAD_UNLOCK_OPERATION: "Keypad unlock operation",
    Event.CREDENTIAL_LOCK_OPEN_OPERATION: "Credential lock/open operation",
    Event.CREDENTIAL_UNLOCK_CLOSE_OPERATION: "Credential unlock/close operation",
    Event.AUTO_LOCK_LOCKED_OPERATION: "Auto lock locked operation",
    Event.SINGLE_USER_CODE_DELETED: "Single user code deleted",
    Event.NEW_USER_CODE_ADDED: "New user code added",
    Event.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE: (
        "New user code not added due to duplicate code"
    ),
    Event.INVALID_CREDENTIAL_USED: "Invalid credential used",
    Event.MANUALLY_ENTER_USER_ACCESS_CODE_EXCEEDS_CODE_LIMIT: (
        "Manually enter user access code exceeds code limit"
    ),
    Event.UNLOCK_BY_RF_WITH_INVALID_USER_CODE: "Unlock by RF with invalid user code",
    Event.LOCKED_BY_RF_WITH_INVALID_USER_CODE: "Locked by RF with invalid user code",
    Event.LOCK_OPERATION_WITH_USER_CODE: "Lock operation with User Code",
    Event.UNLOCK_OPERATION_WITH_USER_CODE: "Unlock operation with User Code",
}

# The user every case names: a slot both providers' entries configure.
USER = 1

# How the driver hands on the user an event names. A keypad operation's is a
# User Code Report's ``userId`` (UCC:1626-1628), or the lone User ID byte some
# locks send instead (NCC:1480-1493). An operation with User Code's is its
# registry-named value parameter, ``user ID`` (NOTIF:833-855, NCC:1594-1612).
_USER_ID = {"userId": USER}
_USER_ID_VALUE = {"user ID": USER}

# Events naming a Personal Identification Number use: the event, its
# parameters, and whether it left the lock locked.
PIN_USE_CASES = [
    pytest.param(Event.KEYPAD_LOCK_OPERATION, _USER_ID, True, id="keypad_lock"),
    pytest.param(Event.KEYPAD_UNLOCK_OPERATION, _USER_ID, False, id="keypad_unlock"),
    pytest.param(
        Event.LOCK_OPERATION_WITH_USER_CODE,
        _USER_ID_VALUE,
        True,
        id="lock_with_user_code",
    ),
    pytest.param(
        Event.UNLOCK_OPERATION_WITH_USER_CODE,
        _USER_ID_VALUE,
        False,
        id="unlock_with_user_code",
    ),
]

# Events that are no use of a credential: the event and its parameters.
# ``None`` is an event the registry gives no parameters (NOTIF:651-672, 745,
# 797-809). A user on such an event is the worst case, what a lock sending
# more than the registry defines would produce: the event alone must keep it
# from being reported.
NOT_A_USE_CASES: list[Any] = [
    # Turned by hand, or locked by the lock's own timer: no credential.
    pytest.param(Event.MANUAL_LOCK_OPERATION, _USER_ID, id="manual_lock"),
    pytest.param(Event.MANUAL_UNLOCK_OPERATION, _USER_ID, id="manual_unlock"),
    pytest.param(Event.AUTO_LOCK_LOCKED_OPERATION, _USER_ID, id="auto_lock"),
    # A Z-Wave command: the registry gives it no user, so it carried no code.
    pytest.param(Event.RF_LOCK_OPERATION, None, id="rf_lock"),
    pytest.param(Event.RF_UNLOCK_OPERATION, None, id="rf_unlock"),
    pytest.param(Event.RF_UNLOCK_OPERATION, _USER_ID, id="rf_unlock_naming_a_user"),
    # A keypad operation naming no user.
    pytest.param(Event.KEYPAD_UNLOCK_OPERATION, {"userId": 0}, id="keypad_user_0"),
    pytest.param(
        Event.KEYPAD_UNLOCK_OPERATION, {"userId": None}, id="keypad_user_null"
    ),
    # Failed attempts.
    pytest.param(
        Event.UNLOCK_BY_RF_WITH_INVALID_USER_CODE,
        _USER_ID,
        id="unlock_by_rf_with_invalid_user_code",
    ),
    pytest.param(
        Event.LOCKED_BY_RF_WITH_INVALID_USER_CODE,
        _USER_ID,
        id="locked_by_rf_with_invalid_user_code",
    ),
    pytest.param(Event.INVALID_CREDENTIAL_USED, None, id="invalid_credential_used"),
    pytest.param(
        Event.MANUALLY_ENTER_USER_ACCESS_CODE_EXCEEDS_CODE_LIMIT,
        None,
        id="code_limit_exceeded",
    ),
    # Programming: no write in flight, so nothing else answers these.
    pytest.param(
        Event.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE, _USER_ID, id="duplicate"
    ),
    pytest.param(Event.NEW_USER_CODE_ADDED, _USER_ID, id="code_added"),
    pytest.param(Event.SINGLE_USER_CODE_DELETED, _USER_ID, id="code_deleted"),
    # The driver parses a version 8 credential operation's parameters only as
    # a Command Class (NCC:1474-1593), which its Credential Usage Data is not,
    # so no credential type reaches either provider.
    pytest.param(
        Event.CREDENTIAL_UNLOCK_CLOSE_OPERATION, None, id="credential_unlock_unparsed"
    ),
]
