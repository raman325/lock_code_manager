"""
What the Z-Wave providers share about a lock's Access Control notifications.

Z-Wave JS hears a notification as a Home Assistant bus event keyed by event
number; Z-Wave JS UI as an MQTT topic named by the event's label. Both read
the rule for which notifications are a credential use, and whose, from here.
"""

from collections.abc import Mapping
from typing import NamedTuple

from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
)

from ._util import parse_slot_num


class PinUse(NamedTuple):
    """An Access Control event that is a PIN use, as the driver labels it."""

    # The driver's label (node-zwave-js packages/core/src/registries/
    # Notifications.ts), which is all Z-Wave JS UI names an event by.
    label: str
    # Whether the operation left the lock locked.
    to_locked: bool


# A use counts as the credential its Access Control event names, and belongs
# to the user the event names; an event naming no user is not a use. The
# keypad operations and the version 8 operations "with User Code" name a
# Personal Identification Number. Every other event names no credential, so
# it is not reported: manual, auto-lock, a radio frequency operation (a
# Z-Wave command, which the registry gives no user), a failed or invalid-code
# attempt, or a programming event such as a duplicate refusal. The version 8
# credential operations would name theirs, but the driver does not parse
# their parameters, so no credential type reaches a provider.
ACCESS_CONTROL_PIN_USES: dict[AccessControlNotificationEvent, PinUse] = {
    AccessControlNotificationEvent.KEYPAD_LOCK_OPERATION: PinUse(
        "Keypad lock operation", to_locked=True
    ),
    AccessControlNotificationEvent.KEYPAD_UNLOCK_OPERATION: PinUse(
        "Keypad unlock operation", to_locked=False
    ),
    AccessControlNotificationEvent.LOCK_OPERATION_WITH_USER_CODE: PinUse(
        "Lock operation with User Code", to_locked=True
    ),
    AccessControlNotificationEvent.UNLOCK_OPERATION_WITH_USER_CODE: PinUse(
        "Unlock operation with User Code", to_locked=False
    ),
}

# The lock refused a PIN another slot holds. Not a use; it answers the write
# in flight when it names no slot or the written one.
ACCESS_CONTROL_DUPLICATE = (
    AccessControlNotificationEvent.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE
)

# The driver's label for every event a provider acts on.
ACCESS_CONTROL_EVENT_LABELS: dict[AccessControlNotificationEvent, str] = {
    **{event: use.label for event, use in ACCESS_CONTROL_PIN_USES.items()},
    ACCESS_CONTROL_DUPLICATE: "New user code not added due to duplicate code",
}

# Where the driver puts the user an event names: a keypad operation's User
# Code Report as ``userId``, an operation with User Code's value parameter
# under the registry's name for it, ``user ID``.
_USER_KEYS = ("userId", "user ID")


def access_control_user(parameters: object) -> int | None:
    """
    Return the user an Access Control notification's parameters name, or None.

    Parameters that are not a mapping (a raw buffer, nothing at all), a user
    that is not a whole number, and user 0 all name nobody.
    """
    if not isinstance(parameters, Mapping):
        return None
    user = parse_slot_num(
        next((parameters[key] for key in _USER_KEYS if key in parameters), None)
    )
    return user if user is not None and user > 0 else None
