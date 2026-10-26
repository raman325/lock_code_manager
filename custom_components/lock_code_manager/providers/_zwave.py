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

from ._util import refuses_write_in_flight
from .const import LOGGER

# A use counts as the credential its Access Control event names, and belongs
# to the user the event names; an event naming no user is not a use. The
# keypad operations and the version 8 operations "with User Code" name a
# Personal Identification Number. Every other event names no credential, so
# it is not reported: manual, auto-lock, a radio frequency operation (a
# Z-Wave command, which the registry gives no user), a failed or invalid-code
# attempt, or a programming event such as a duplicate refusal. Each entry says
# whether the operation left the lock locked.
ACCESS_CONTROL_PIN_USE_TO_LOCKED: dict[AccessControlNotificationEvent, bool] = {
    AccessControlNotificationEvent.KEYPAD_LOCK_OPERATION: True,
    AccessControlNotificationEvent.LOCK_OPERATION_WITH_USER_CODE: True,
    AccessControlNotificationEvent.KEYPAD_UNLOCK_OPERATION: False,
    AccessControlNotificationEvent.UNLOCK_OPERATION_WITH_USER_CODE: False,
}

# Where the driver puts the user an event names: a keypad operation's User
# Code Report as ``userId``, an operation with User Code's value parameter
# under the registry's name for it, ``user ID``.
_USER_KEYS = ("userId", "user ID")


class AccessControlUser(NamedTuple):
    """The user an Access Control notification names."""

    # The user, or None when the notification names none or one that is no
    # user identifier.
    user: int | None
    # Whether it names something that is no user identifier: not a whole
    # number, or below zero. A duplicate refusal naming that answers no write,
    # where one naming no user may answer the write in flight.
    unreadable: bool = False


def access_control_user(parameters: object) -> AccessControlUser:
    """
    Return the user an Access Control notification's parameters name.

    No parameters, parameters that are not a mapping (a raw buffer), no user
    key, a null user and user 0 all name no user. Anything else that is not
    a whole number above zero is unreadable: a fraction is not truncated to a
    slot, nor is text or a boolean coerced to one.
    """
    if not isinstance(parameters, Mapping):
        return AccessControlUser(None)
    value = next((parameters[key] for key in _USER_KEYS if key in parameters), None)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if value is None or (type(value) is int and value == 0):
        return AccessControlUser(None)
    if type(value) is not int or value < 0:
        return AccessControlUser(None, unreadable=True)
    return AccessControlUser(value)


def answers_write_in_flight(write_slot: int | None, named: AccessControlUser) -> bool:
    """
    Return whether a duplicate refusal naming this user answers the write in flight.

    One naming no user, or the written slot, does (``refuses_write_in_flight``);
    one naming something that is no user identifier answers nothing.
    """
    return not named.unreadable and refuses_write_in_flight(write_slot, named.user)


def log_not_a_use(entity_id: str, event: str, named: AccessControlUser) -> None:
    """Record an Access Control notification that is not reported as a use."""
    LOGGER.debug(
        "Lock %s: Access Control notification %s naming user %s is not a "
        "credential use, so it is not reported",
        entity_id,
        event,
        "unreadable" if named.unreadable else named.user,
    )
