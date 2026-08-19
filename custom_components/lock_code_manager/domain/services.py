"""LCM service-handler implementations."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_CONDITION, CONF_ENABLED, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from ..const import DOMAIN, EXCLUDED_CONDITION_PLATFORMS
from .allocation import SlotAllocationError, async_allocate_for
from .config import EntryConfig
from .locks import get_managed_lock
from .names import identity, name_error, normalize_name
from .queries import get_entry_config, get_loaded_config_entry


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
    hass.config_entries.async_update_entry(config_entry, options=new_config.to_dict())


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
    hass.config_entries.async_update_entry(config_entry, options=new_config.to_dict())


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
            hass, config.locks, len(config.users) + 1, excluding=config_entry
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
    hass.config_entries.async_update_entry(
        config_entry,
        options=EntryConfig(
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
    hass.config_entries.async_update_entry(
        config_entry,
        options=EntryConfig(
            locks=config.locks,
            users=remaining,
            assignment=config.assignment.reconcile(remaining, start=1),
            extra=config.extra,
        ).to_dict(),
    )
