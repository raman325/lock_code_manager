"""Text for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_SLOTS, DOMAIN
from .entity import BaseLockCodeManagerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_standard_text_entities(slot_num: int) -> None:
        """Add standard text entities for slot."""
        async_add_entities(
            [
                LockCodeManagerText(hass, config_entry, slot_num, *props)
                for props in ((CONF_NAME, TextMode.TEXT), (CONF_PIN, TextMode.PASSWORD))
            ],
            True,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_{config_entry.entry_id}_add", add_standard_text_entities
        )
    )

    return True


class LockCodeManagerText(BaseLockCodeManagerEntity, TextEntity):
    """Text entity for lock code manager."""

    _attr_native_min = 1
    _attr_native_max = 9999
    _enabled_entity_id: str = ""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        slot_num: int,
        key: str,
        text_mode: TextMode,
    ) -> None:
        """Initialize Text entity."""
        BaseLockCodeManagerEntity.__init__(self, hass, config_entry, slot_num, key)
        self._attr_mode = text_mode

    @property
    def native_value(self) -> str | None:
        """Return native value."""
        entry = self.config_entry
        return entry.data[CONF_SLOTS][self.slot_num].get(self.key)

    async def async_set_value(self, value: str) -> None:
        """Set value of text."""
        if not self._enabled_entity_id:
            self._enabled_entity_id = self.ent_reg.async_get_entity_id(
                Platform.SWITCH, DOMAIN, self._get_uid(CONF_ENABLED)
            )
        if (
            self.key == CONF_PIN
            and not value.strip()
            and self._enabled_entity_id
            and (state := self.hass.states.get(self._enabled_entity_id))
            and state.state
        ):
            async_create(
                self.hass,
                (
                    f"PIN must be a valid value because slot {self.slot_num} is "
                    f"enabled on the lock configuration {self.config_entry.title}."
                ),
                "Problem with Lock Code Manager",
                f"{DOMAIN}_{self.config_entry.entry_id}_{self.slot_num}_pin_required",
            )
            return
        self._update_config_entry(value)

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        await TextEntity.async_added_to_hass(self)
