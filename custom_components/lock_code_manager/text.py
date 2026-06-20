"""Text for lock_code_manager."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .domain.credentials import CredentialType, aggregate_length_bounds
from .domain.models import LockCodeManagerConfigEntry
from .entity import BaseLockCodeManagerEntity

if TYPE_CHECKING:
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)

# The single credential-type-specific knob. A text entity's ``key`` maps to a
# credential type when its value length is governed by a lock capability;
# keys absent here (for example the slot name) carry no length constraint.
# Adding password support later is a one-line entry once a password entity exists.
CREDENTIAL_TYPE_BY_CONF_KEY: Mapping[str, CredentialType] = {
    CONF_PIN: CredentialType.PIN,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_standard_text_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add standard text entities for slot."""
        async_add_entities(
            [
                LockCodeManagerText(hass, ent_reg, config_entry, slot_num, *props)
                for props in ((CONF_NAME, TextMode.TEXT), (CONF_PIN, TextMode.PASSWORD))
            ],
            True,
        )

    config_entry.async_on_unload(
        config_entry.runtime_data.callbacks.register_standard_adder(
            add_standard_text_entities
        )
    )

    return True


class LockCodeManagerText(BaseLockCodeManagerEntity, TextEntity):
    """Text entity for lock code manager."""

    # Defaults for keys with no length constraint (the slot name) and the
    # fallback when bound locks advertise nothing or an unsatisfiable range.
    _DEFAULT_MIN = 0
    _DEFAULT_MAX = 9999

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
        key: str,
        text_mode: TextMode,
    ) -> None:
        """Initialize Text entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, ent_reg, config_entry, slot_num, key
        )
        self._attr_mode = text_mode

    @property
    def native_min(self) -> int:
        """Return the minimum value length advertised by the bound locks."""
        return self._bounds()[0]

    @property
    def native_max(self) -> int:
        """Return the maximum value length advertised by the bound locks."""
        return self._bounds()[1]

    def _bounds(self) -> tuple[int, int]:
        """
        Compute the live tightest-common length range across the bound locks.

        Reads each lock's synchronously cached capabilities (uncached or
        disconnected locks contribute nothing). Non-credential keys and an
        unsatisfiable intersection both fall back to the default range so the
        control is never rendered inverted; the coordinator gate reports the
        real per-lock conflict when a PIN is actually set.
        """
        credential_type = CREDENTIAL_TYPE_BY_CONF_KEY.get(self.key)
        if credential_type is None:
            return (self._DEFAULT_MIN, self._DEFAULT_MAX)
        lo, hi = aggregate_length_bounds(
            (lock.cached_capabilities for lock in self.locks), credential_type
        )
        lo = self._DEFAULT_MIN if lo is None else lo
        hi = self._DEFAULT_MAX if hi is None else hi
        if lo > hi:
            lo, hi = self._DEFAULT_MIN, self._DEFAULT_MAX
        # Home Assistant validates the stored value against these bounds when
        # it renders state and raises if the value falls outside, so the
        # advertised range must always admit the current value: the empty
        # string after a clear (length 0, which would fail any positive
        # minimum), and any PIN written before the lock advertised its limits.
        # The coordinator gate -- not these display hints -- rejects new
        # out-of-range input.
        value = self.native_value
        if value is not None:
            lo = min(lo, len(value))
            hi = max(hi, len(value))
        return (lo, hi)

    @callback
    def _handle_add_locks(self, locks: list[BaseLock]) -> None:
        """Refresh advertised bounds when locks are added to the slot."""
        super()._handle_add_locks(locks)
        self._write_bounds_update()

    @callback
    def _handle_remove_lock(self, lock_entity_id: str) -> None:
        """Refresh advertised bounds when a lock is removed from the slot."""
        super()._handle_remove_lock(lock_entity_id)
        self._write_bounds_update()

    @callback
    def _write_bounds_update(self) -> None:
        """Re-push state so the frontend re-reads the length bounds."""
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        """Return native value."""
        return self._state

    async def async_set_value(self, value: str) -> None:
        """Set value of text."""
        coordinator = self._require_slot_coordinator()
        if self.key == CONF_PIN:
            await coordinator.async_request_pin_update(value)
        else:
            await coordinator.async_request_name_update(value)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        await TextEntity.async_added_to_hass(self)
