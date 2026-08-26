"""Event entity for lock_code_manager."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.const import ATTR_NAME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    BUS_EVENT_CREDENTIAL_USED,
    EVENT_CREDENTIAL_USED,
)
from .domain.models import LockCodeManagerConfigEntry
from .domain.queries import get_entry_config
from .entity import BaseLockCodeManagerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_code_slot_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add code slot event entities for slot."""
        async_add_entities(
            [
                LockCodeManagerCodeSlotEventEntity(
                    hass, ent_reg, config_entry, slot_num, EVENT_CREDENTIAL_USED
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        config_entry.runtime_data.callbacks.register_standard_adder(
            add_code_slot_entities
        )
    )
    return True


class LockCodeManagerCodeSlotEventEntity(BaseLockCodeManagerEntity, EventEntity):
    """
    Records every use of this user's credential, wherever it happened.

    One event type, ``credential_used``, always. Home Assistant refuses an
    event type an entity did not declare, so a vocabulary that answered
    "where" -- the entry's lock entity IDs, say -- makes a use against
    anything outside it impossible to record at all. Keep it saying only
    "what": where the credential was used is the payload's ``target``,
    which nothing has to admit in advance.
    """

    _attr_entity_category = None
    _attr_translation_key = EVENT_CREDENTIAL_USED

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
        key: str,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, ent_reg, config_entry, slot_num, key
        )
        self._attr_event_types = [EVENT_CREDENTIAL_USED]

    @callback
    def _credential_used_filter(self, event_data: dict[str, Any]) -> bool:
        """
        Keep the unified events this slot's entity is the one to record.

        The unified event names the person rather than the slot number they
        occupy, so working out whose entity this is means asking the
        configuration who holds this slot right now.

        Deliberately says nothing about ``target``. A use against something
        this integration does not manage -- a cover, an alarm panel, a
        keypad with no integration of its own -- is still this user's
        credential being used, and recording it is what this entity is for.
        """
        slot_user = get_entry_config(self.config_entry).name_for(self.slot_num)
        return (
            event_data[ATTR_CONFIG_ENTRY_ID] == self.entry_id
            and event_data[ATTR_NAME] == slot_user
        )

    @callback
    def _handle_credential_used(self, event: Event) -> None:
        """
        Record the use, publishing the unified payload as state attributes.

        The whole payload, unedited: a consumer reads ``target`` for where
        the credential was used and ``source`` for where it was entered,
        which for a use a lock observed itself are the same entity.
        """
        self._trigger_event(EVENT_CREDENTIAL_USED, event.data)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        # EventEntity.async_added_to_hass restores __last_event_type from stored data
        await EventEntity.async_added_to_hass(self)

        # The unified event is the only source. Every use reaches it --
        # observed by a lock or reported through the ``use_credential``
        # action -- so there is one recording path and nothing to
        # de-duplicate between two of them.
        self.async_on_remove(
            self.hass.bus.async_listen(
                BUS_EVENT_CREDENTIAL_USED,
                self._handle_credential_used,
                self._credential_used_filter,
            )
        )
