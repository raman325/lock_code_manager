"""LCM service-handler implementations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CONDITION, CONF_ENABLED, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from ..const import (
    ATTR_REASON,
    ATTR_USER,
    ATTR_VALID,
    DOMAIN,
    EXCLUDED_CONDITION_PLATFORMS,
)
from .allocation import SlotAllocationError, async_allocate_for
from .config import EntryConfig
from .events import async_fire_credential_used
from .locks import get_managed_lock
from .names import identity, name_error, normalize_name
from .queries import get_entry_config, get_loaded_config_entry
from .validation import validate_credential

_LOGGER = logging.getLogger(__name__)

# How long to wait for the entry to react to a write before returning
# anyway. Generous, because the pass can set up a lock it has never
# spoken to; a caller stuck here is waiting on that, not on this.
_SETTLE_TIMEOUT = 30


async def _async_write_and_settle(
    hass: HomeAssistant, config_entry: ConfigEntry, options: dict[str, Any]
) -> None:
    """
    Write the entry, and wait for it to have finished reacting.

    ``async_update_entry`` schedules the update listener rather than
    awaiting it, so without this a service returns before the entities for
    the users it changed exist. A script that adds a user and then sets
    their PIN through the new text entity would find nothing to set.

    Waiting on the entry's settle event rather than on the specific
    entities keeps this honest about what it can promise: another write
    landing at the same time can release the wait early. That is no worse
    than not waiting, which is the alternative.
    """
    runtime_data = config_entry.runtime_data
    runtime_data.settled.clear()
    if not hass.config_entries.async_update_entry(config_entry, options=options):
        # Nothing changed, so no listener will run and nothing will set the
        # event. Waiting here would burn the whole timeout for no reason.
        return
    try:
        async with asyncio.timeout(_SETTLE_TIMEOUT):
            await runtime_data.settled.wait()
    except TimeoutError:
        # The write itself is durable, so this is not a failure to report to
        # the caller -- the entities will appear when the pass finishes.
        _LOGGER.warning(
            "%s (%s): entry did not finish updating within %ss; entities for "
            "this change may appear late",
            config_entry.entry_id,
            config_entry.title,
            _SETTLE_TIMEOUT,
        )


async def async_set_usercode(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int, usercode: str
) -> None:
    """Set a usercode on a lock slot."""
    usercode = usercode.strip()
    if not usercode:
        raise ServiceValidationError(
            "Usercode must not be empty; use the clear operation instead"
        )
    lock = get_managed_lock(hass, lock_entity_id)
    await lock.async_internal_set_usercode(code_slot, usercode)


async def async_clear_usercode(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int
) -> None:
    """Clear a usercode from a lock slot."""
    lock = get_managed_lock(hass, lock_entity_id)
    await lock.async_internal_clear_usercode(code_slot)


async def async_use_credential(
    hass: HomeAssistant,
    code: str,
    *,
    source: str,
    target: str,
    config_entry_id: str | None = None,
    config_entry_title: str | None = None,
) -> dict[str, Any]:
    """
    Report a credential use to one entry, and answer whether it was valid.

    For the keypads and door controllers Lock Code Manager cannot watch: the
    caller says what was entered and where, and this answers the same
    question the entry's own dashboard would while recording the use.

    A valid code announces itself on the unified credential-used event. An
    invalid one announces nothing at all -- the response is the whole answer,
    so an automation that wants to react to a rejection does so from the
    response rather than from an event anyone else could see.

    The entry is the target rather than a lock because an entry's users are
    what a code is checked against, and a code that no lock has ever been
    programmed with still has an answer here.
    """
    config_entry = get_loaded_config_entry(hass, config_entry_id, config_entry_title)
    result = validate_credential(config_entry, code)

    # ``user`` is set exactly when the credential validated; reading it
    # rather than ``valid`` is also what tells the type checker it is there.
    if result.user is not None:
        # One event, whatever the target is. The entry's per-slot event
        # entity reads this off the bus and records the use itself when the
        # target is one of its event-capable locks, so nothing here has to
        # know which targets are recordable.
        #
        # ``source`` and ``target`` are data. Nothing here dereferences them,
        # looks them up in a registry, or reads their state: a code source's
        # state can be the cleartext credential that was just typed.
        async_fire_credential_used(
            hass, config_entry, name=result.user, source=source, target=target
        )

    return {
        ATTR_VALID: result.valid,
        ATTR_USER: result.user,
        ATTR_REASON: result.reason,
    }


def _async_validate_condition(hass: HomeAssistant, condition: str) -> None:
    """Refuse a condition entity this integration cannot read."""
    if not hass.states.get(condition):
        raise ServiceValidationError(f"Entity {condition} not found")

    ent_reg = er.async_get(hass)
    entity_entry = ent_reg.async_get(condition)
    if entity_entry and entity_entry.platform in EXCLUDED_CONDITION_PLATFORMS:
        raise ServiceValidationError(
            f"Entities from the '{entity_entry.platform}' integration are not "
            "supported as condition entities. See the wiki for details: "
            "https://github.com/raman325/lock_code_manager/wiki/"
            "Unsupported-Condition-Entity-Integrations"
        )


async def async_set_slot_condition(
    hass: HomeAssistant,
    slot: int,
    entity_id: str,
    *,
    config_entry_id: str | None = None,
    config_entry_title: str | None = None,
) -> None:
    """Set a condition entity for a slot."""
    config_entry = get_loaded_config_entry(hass, config_entry_id, config_entry_title)
    config = get_entry_config(config_entry)
    if not config.has_slot(slot):
        raise ServiceValidationError(f"Slot {slot} not found in config entry")

    _async_validate_condition(hass, entity_id)

    new_config = config.with_slot_field_set(slot, CONF_CONDITION, entity_id)
    await _async_write_and_settle(hass, config_entry, new_config.to_dict())


async def async_clear_slot_condition(
    hass: HomeAssistant,
    slot: int,
    *,
    config_entry_id: str | None = None,
    config_entry_title: str | None = None,
) -> None:
    """Clear the condition entity from a slot."""
    config_entry = get_loaded_config_entry(hass, config_entry_id, config_entry_title)
    config = get_entry_config(config_entry)
    if not config.has_slot(slot):
        raise ServiceValidationError(f"Slot {slot} not found in config entry")

    new_config = config.with_slot_field_removed(slot, CONF_CONDITION)
    await _async_write_and_settle(hass, config_entry, new_config.to_dict())


async def async_add_user(
    hass: HomeAssistant,
    name: str,
    *,
    config_entry_id: str | None = None,
    config_entry_title: str | None = None,
    pin: str | None = None,
    enabled: bool = True,
    condition: str | None = None,
) -> None:
    """
    Add a user to an entry, on a slot number chosen by reading the locks.

    The caller names the person and nothing else. Allocation runs against
    the entry's own locks, through the same path the config flow uses, so a
    user added by service and one added in the editor cannot land on
    different numbers or disagree about which are free.
    """
    config_entry = get_loaded_config_entry(hass, config_entry_id, config_entry_title)
    config = get_entry_config(config_entry)

    if error := name_error(name):
        raise ServiceValidationError(f"Invalid name {name!r}: {error}")
    name = normalize_name(name)
    # Two names meaning one person would collapse into a single key on the
    # way into storage, taking one of their credentials with it.
    if any(identity(known) == identity(name) for known in config.users):
        raise ServiceValidationError(f"A user named {name!r} already exists")

    # The same rule the editor enforces: a slot cannot be programmed without
    # something to program.
    if enabled and not pin:
        raise ServiceValidationError(f"{name!r} cannot be enabled without a PIN")

    if condition:
        _async_validate_condition(hass, condition)

    user: dict[str, Any] = {CONF_ENABLED: enabled}
    if pin:
        user[CONF_PIN] = pin
    if condition:
        user[CONF_CONDITION] = condition

    try:
        unavailable = await async_allocate_for(
            hass, config_entry, config.locks, len(config.users) + 1
        )
    except SlotAllocationError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.translation_key,
            translation_placeholders={
                key: str(value) for key, value in err.placeholders.items()
            },
        ) from err

    # Reconciled against the whole set rather than issued directly: everyone
    # already here keeps their number by tenure, and only the newcomer is
    # given one.
    assignment = config.assignment.reconcile(
        [*config.users, name], start=1, unavailable=unavailable
    )
    await _async_write_and_settle(
        hass,
        config_entry,
        EntryConfig(
            locks=config.locks,
            users={**config.users, name: user},
            assignment=assignment,
            extra=config.extra,
        ).to_dict(),
    )


async def async_delete_user(
    hass: HomeAssistant,
    name: str,
    *,
    config_entry_id: str | None = None,
    config_entry_title: str | None = None,
    clear_credentials: bool = True,
) -> None:
    """
    Remove a user from an entry, and by default from the locks.

    ``clear_credentials=False`` hands the credential over rather than
    deleting it: Lock Code Manager stops managing the slot and whatever is
    programmed there keeps working. The intent cannot ride in the new
    configuration, because the user it concerns is precisely what the new
    configuration no longer has, so it is left for the update listener to
    pick up as it processes this write.
    """
    config_entry = get_loaded_config_entry(hass, config_entry_id, config_entry_title)
    config = get_entry_config(config_entry)

    stored = next(
        (known for known in config.users if identity(known) == identity(name)), None
    )
    if stored is None:
        raise ServiceValidationError(f"No user named {name!r} in this config entry")

    if not clear_credentials and (slot_num := config.assignment.slot(stored)):
        config_entry.runtime_data.retained_pairs.update(
            (lock_entity_id, slot_num) for lock_entity_id in config.locks
        )

    remaining = {
        known: fields for known, fields in config.users.items() if known != stored
    }
    # No unavailable set and so no lock read: a departure issues no numbers,
    # and everyone remaining keeps theirs by tenure.
    await _async_write_and_settle(
        hass,
        config_entry,
        EntryConfig(
            locks=config.locks,
            users=remaining,
            assignment=config.assignment.reconcile(remaining, start=1),
            extra=config.extra,
        ).to_dict(),
    )
