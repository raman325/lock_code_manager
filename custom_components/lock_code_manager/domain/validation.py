"""Validate a submitted credential against an entry's desired state."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.const import CONF_CONDITION
from homeassistant.core import callback

from ..const import (
    REASON_CONDITION_NOT_MET,
    REASON_PRECEDENCE,
    REASON_UNKNOWN_CODE,
    REASON_USER_DISABLED,
)
from .models import LockCodeManagerConfigEntry
from .queries import get_entry_config
from .slot_coordinator import SlotEntityCoordinator


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating one submitted credential."""

    valid: bool
    user: str | None
    reason: str | None


def _failure_reason(matches: list[SlotEntityCoordinator]) -> str:
    """
    Explain why no matched slot accepted the credential.

    The most restrictive reason wins: a slot held back by anything beyond
    its condition entity (the enabled flag, for one) reads as disabled even
    if another slot with the same code is merely waiting on its condition.
    """
    if not matches:
        return REASON_UNKNOWN_CODE
    return max(
        (
            REASON_CONDITION_NOT_MET
            if reason == CONF_CONDITION
            else REASON_USER_DISABLED
            for coordinator in matches
            for reason in coordinator.inactive_because_of
        ),
        key=REASON_PRECEDENCE.index,
        # A matched slot with no recorded reason is inactive all the same,
        # and lands on the least restrictive explanation available.
        default=REASON_CONDITION_NOT_MET,
    )


@callback
def validate_credential(
    config_entry: LockCodeManagerConfigEntry,
    code: str,
) -> ValidationResult:
    """
    Validate ``code`` against the entry's configured users.

    A pure query: it reports what the entry's configuration says about the
    code and does nothing else -- no lock is contacted, nothing is written,
    and no event is fired. It deliberately takes no ``hass``: firing an event
    or reaching a device would need one, so its absence keeps this a question
    rather than something that can grow side effects. The active check is the slot coordinator's own
    derived state -- the same predicate the active binary sensor renders --
    so a validation and the dashboard can never disagree about whether a
    credential works.

    Normalizing here rather than at each entry point is what makes "one
    validation function" true: a keypad that appends a newline has to get
    the same answer as the service call that trims one.
    """
    code = code.strip()
    coordinators = config_entry.runtime_data.slot_coordinators
    matches = [c for c in coordinators.values() if c.pin_value == code]
    # A coordinator whose active state was never computed (``is_active`` is
    # None, empty inactive reasons) counts as inactive and folds into
    # condition_not_met -- unreachable after entry setup, stated so a
    # pre-start caller isn't surprised.
    active = next((c for c in matches if c.is_active), None)

    if active is None:
        return ValidationResult(valid=False, user=None, reason=_failure_reason(matches))

    name = get_entry_config(config_entry).name_for(active.slot_num)
    return ValidationResult(valid=True, user=name, reason=None)
