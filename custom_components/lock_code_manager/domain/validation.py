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
    """
    Outcome of validating one submitted credential.

    ``user`` is set exactly when ``valid`` is True, and ``reason`` exactly
    when it is False. The slot number the credential sits on is deliberately
    absent: it is internal bookkeeping, and the use is announced by the
    user's name so that no consumer ends up keyed on a number.
    """

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
    # ``max`` needs no default: every match reaching here is inactive, and a
    # coordinator that has recomputed derives ``is_active`` as the negation of
    # ``inactive_because_of``, so it names at least one reason. Decouple those
    # two, or reach a coordinator before its first recompute, and this raises
    # on an empty sequence.
    return max(
        (
            REASON_CONDITION_NOT_MET
            if reason == CONF_CONDITION
            else REASON_USER_DISABLED
            for coordinator in matches
            for reason in coordinator.inactive_because_of
        ),
        key=REASON_PRECEDENCE.index,
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
    and no event is fired. The active check is the slot coordinator's own
    derived state -- the same predicate the active binary sensor renders --
    so a validation and the dashboard can never disagree about whether a
    credential works.

    Normalizing here rather than at each entry point is what makes "one
    validation function" true: every caller gets the same answer for codes
    that differ only in surrounding whitespace.
    """
    code = code.strip()
    coordinators = config_entry.runtime_data.slot_coordinators
    matches = [c for c in coordinators.values() if c.pin_value == code]
    active = next((c for c in matches if c.is_active), None)

    if active is None:
        return ValidationResult(valid=False, user=None, reason=_failure_reason(matches))

    name = get_entry_config(config_entry).name_for(active.slot_num)
    return ValidationResult(valid=True, user=name, reason=None)
