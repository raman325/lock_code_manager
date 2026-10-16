"""
Per-lock naming in Home Assistant.

The name a person sees for a lock, and the ids of the repair issues raised
about one lock.
"""

from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er


@callback
def lock_display_name(hass: HomeAssistant, lock_entity_id: str) -> str:
    """
    Name a lock the way Home Assistant does.

    The friendly name first, because that is the string on screen and the
    one a person would use to say which lock they mean. Everything after
    it reconstructs that name for a lock with no state yet, in the order
    Home Assistant composes it.

    The device step is not a nicety. A lock entity that sets
    ``has_entity_name`` with no name of its own -- which is how Z-Wave,
    Matter and most modern integrations do it -- carries an empty
    ``original_name`` and takes its whole visible name from its device.
    Stopping at the registry row names such a lock ``lock.front_door``.
    """
    if (state := hass.states.get(lock_entity_id)) and (
        friendly_name := state.attributes.get(ATTR_FRIENDLY_NAME)
    ):
        return str(friendly_name)
    if entity := er.async_get(hass).async_get(lock_entity_id):
        if name := entity.name or entity.original_name:
            return name
        if (
            entity.device_id
            and (device := dr.async_get(hass).async_get(entity.device_id))
            and (device_name := device.name_by_user or device.name)
        ):
            return device_name
    # Nothing left to ask, so fall back to the object id, which is what the
    # lock's own entity id was slugged from in the first place.
    return lock_entity_id.split(".", 1)[-1].replace("_", " ")


# Every repair issue keyed on a lock entity id. Creation sites live in
# different layers (lock_offline in the coordinator, lock_setup_failed in
# the provider base), but cleanup on entry removal iterates this tuple —
# add new per-lock issue keys here or their persistent issues orphan when
# the entry is deleted.
PER_LOCK_ISSUE_KEYS = ("lock_offline", "lock_setup_failed", "lock_dropped")


def per_lock_issue_id(key: str, lock_entity_id: str) -> str:
    """Build the repair issue id for a per-lock issue key."""
    return f"{key}_{lock_entity_id}"
