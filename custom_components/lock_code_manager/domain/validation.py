"""Validate a submitted credential against an entry's desired state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, CONF_CONDITION
from homeassistant.core import HomeAssistant

from ..const import (
    ATTR_CODE,
    ATTR_LCM_CONFIG_ENTRY_ID,
    ATTR_REASON,
    EVENT_CODE_VALIDATION_FAILED,
    REASON_CONDITION_NOT_MET,
    REASON_UNKNOWN_CODE,
    REASON_USER_DISABLED,
)
from .models import LockCodeManagerConfigEntry
from .queries import get_entry_config
from .slot_coordinator import SlotEntityCoordinator

if TYPE_CHECKING:
    from ..providers import BaseLock


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
    if any(
        reason != CONF_CONDITION
        for coordinator in matches
        for reason in coordinator.inactive_because_of
    ):
        return REASON_USER_DISABLED
    return REASON_CONDITION_NOT_MET


async def async_validate_credential(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    lock: BaseLock,
    code: str,
    *,
    fire_events: bool = True,
) -> ValidationResult:
    """
    Validate ``code`` against the entry's configured users.

    The active check is the slot coordinator's own derived state -- the same
    predicate the active binary sensor renders -- so a reader and the
    dashboard can never disagree about whether a credential works.
    """
    coordinators = config_entry.runtime_data.slot_coordinators
    matches = [c for c in coordinators.values() if c.pin_value == code]
    # A coordinator whose active state was never computed (``is_active`` is
    # None, empty inactive reasons) counts as inactive and folds into
    # condition_not_met -- unreachable after entry setup, stated so a
    # pre-start caller isn't surprised.
    active = next((c for c in matches if c.is_active), None)

    if active is not None:
        name = get_entry_config(config_entry).name_for(active.slot_num)
        if fire_events:
            lock.async_fire_code_slot_event(
                code_slot=active.slot_num,
                action_text="Credential validated",
            )
        return ValidationResult(valid=True, user=name, reason=None)

    reason = _failure_reason(matches)
    if fire_events:
        # Matched codes mask with their slot so the token stays reversible by
        # the deobfuscation map; unknown codes have no slot and stay opaque.
        masked = (
            lock.mask_pin(code, matches[0].slot_num) if matches else lock.mask_pin(code)
        )
        hass.bus.async_fire(
            EVENT_CODE_VALIDATION_FAILED,
            {
                ATTR_ENTITY_ID: lock.lock.entity_id,
                ATTR_DEVICE_ID: lock.lock.device_id,
                ATTR_LCM_CONFIG_ENTRY_ID: config_entry.entry_id,
                ATTR_REASON: reason,
                ATTR_CODE: masked,
            },
        )
    return ValidationResult(valid=False, user=None, reason=reason)
