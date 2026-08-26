"""Validate a submitted credential against an entry's desired state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, CONF_CONDITION
from homeassistant.core import HomeAssistant, callback

from ..const import (
    ATTR_CODE,
    ATTR_LCM_CONFIG_ENTRY_ID,
    ATTR_REASON,
    ATTR_SOURCE_ENTITY_ID,
    DOMAIN,
    EVENT_CODE_VALIDATION_FAILED,
    REASON_CONDITION_NOT_MET,
    REASON_PRECEDENCE,
    REASON_UNKNOWN_CODE,
    REASON_USER_DISABLED,
)
from .models import LockCodeManagerConfigEntry
from .queries import get_entry_config
from .slot_coordinator import SlotEntityCoordinator
from .util import mask_pin

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
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    code: str,
    *,
    lock: BaseLock | None = None,
    fire_events: bool = True,
    source_entity_id: str | None = None,
) -> ValidationResult:
    """
    Validate ``code`` against the entry's configured users.

    The active check is the slot coordinator's own derived state -- the same
    predicate the active binary sensor renders -- so a validation and the
    dashboard can never disagree about whether a credential works.

    ``lock`` names the lock a success should be attributed to, and is what
    makes the success event addressable at all: a slot's event entity keys
    its event type on a lock entity ID, so a success with no lock has
    nowhere to land and fires nothing. A failure is an entry-level fact and
    fires either way, carrying a lock only when one was named.

    ``source_entity_id`` names whatever collected the code, and rides along
    on whichever event this fires. Without it the events name only the lock
    the code was checked against, which a keypad submission and a real
    unlock at the door share.

    Callers rely on this completing without suspension: nothing may
    interleave between a validation and whatever its caller does next.

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

    if active is not None:
        name = get_entry_config(config_entry).name_for(active.slot_num)
        if fire_events and lock is not None:
            # A validated credential is represented as an unlock transition
            # so the standard event surface, which forwards only transitions
            # to unlocked, treats it exactly like a physical PIN unlock.
            lock.async_fire_code_slot_event(
                code_slot=active.slot_num,
                to_locked=False,
                action_text="Credential validated",
                # A dict carrying the ID alone, never the source entity's
                # State: _serialize_source_data publishes a State's own
                # state and attributes, and a code source's state is the
                # cleartext credential that was just typed.
                source_data=(
                    {ATTR_SOURCE_ENTITY_ID: source_entity_id}
                    if source_entity_id is not None
                    else None
                ),
            )
        return ValidationResult(valid=True, user=name, reason=None)

    reason = _failure_reason(matches)
    if fire_events:
        # The module-level masker rather than ``BaseLock.mask_pin``, which is
        # the same function bound to a lock this may not have. Matched codes
        # mask with their slot so the token stays reversible by the
        # deobfuscation map; unknown codes have no slot and stay opaque.
        masked = mask_pin(
            code,
            matches[0].slot_num if matches else 0,
            hass.data.get(DOMAIN, {}).get("instance_id", ""),
        )
        event_data: dict[str, Any] = {
            ATTR_LCM_CONFIG_ENTRY_ID: config_entry.entry_id,
            ATTR_REASON: reason,
            ATTR_CODE: masked,
        }
        # Keys are omitted entirely when unknown, so a template can test for
        # one rather than compare against a placeholder.
        if lock is not None:
            event_data[ATTR_ENTITY_ID] = lock.lock.entity_id
            event_data[ATTR_DEVICE_ID] = lock.lock.device_id
        if source_entity_id is not None:
            event_data[ATTR_SOURCE_ENTITY_ID] = source_entity_id
        hass.bus.async_fire(EVENT_CODE_VALIDATION_FAILED, event_data)
    return ValidationResult(valid=False, user=None, reason=reason)
