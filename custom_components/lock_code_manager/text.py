"""Text for lock_code_manager."""

import logging
from typing import override

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import BaseLockCodeManagerEntity
from .ha.entry import LockCodeManagerConfigEntry, available_credential_types
from .ha.slot_coordinator import InvalidNameError
from .model.credentials import CredentialType
from .model.queries import get_entry_config, subentry_id_for_slot

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""
    callbacks = config_entry.runtime_data.callbacks
    # Awaited adds: a reconcile that returned has added what it decided to.
    platform = entity_platform.async_get_current_platform()

    @callback
    def add_standard_text_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add a slot's name and PIN; its password is the reconcile's."""
        async_add_entities(
            [
                LockCodeManagerNameText(hass, ent_reg, config_entry, slot_num),
                LockCodeManagerCredentialText(
                    hass, ent_reg, config_entry, slot_num, CredentialType.PIN
                ),
            ],
            True,
            config_subentry_id=subentry_id_for_slot(config_entry, slot_num),
        )

    async def reconcile_password_entities() -> None:
        """
        Give each slot a Password entity exactly when the entry offers passwords.

        The one place that decides, and idempotent: it compares the entities
        that exist with what the entry offers now and acts on the difference.
        Removals are awaited before the additions, so a pass that swaps one
        password lock for another never adds beside an entity still leaving.
        A removed entity's stored password stays (spec decision 10).
        """
        slot_numbers = get_entry_config(config_entry).slot_numbers
        wanted = (
            set(slot_numbers)
            if CredentialType.PASSWORD in available_credential_types(config_entry)
            else set()
        )
        present = {
            slot_num
            for slot_num in slot_numbers
            if _password_uid(slot_num) in callbacks.remove_entity
        }
        for slot_num in sorted(present - wanted):
            await callbacks.async_remove_entity(_password_uid(slot_num))
        ent_reg = er.async_get(hass)
        for slot_num in sorted(wanted - present):
            await platform.async_add_entities(
                [
                    LockCodeManagerCredentialText(
                        hass, ent_reg, config_entry, slot_num, CredentialType.PASSWORD
                    )
                ],
                True,
                config_subentry_id=subentry_id_for_slot(config_entry, slot_num),
            )

    config_entry.async_on_unload(
        callbacks.register_standard_adder(add_standard_text_entities)
    )
    config_entry.async_on_unload(
        callbacks.register_entity_reconciler(reconcile_password_entities)
    )
    return True


def _password_uid(slot_num: int) -> str:
    """Return the removal id a slot's Password entity registers under."""
    return f"{slot_num}|{CredentialType.PASSWORD.value}"


class LockCodeManagerText(BaseLockCodeManagerEntity, TextEntity):
    """A slot's text field; each subclass owns its write."""

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
    @override
    def native_min(self) -> int:
        """
        Return the minimum value length -- always the permissive default.

        The advertised per-lock minimum is deliberately NOT surfaced here.
        Home Assistant's ``text.set_value`` service rejects
        ``len(value) < native_min`` before the value reaches the coordinator,
        which would block the empty string that clears a slot and would replace
        the coordinator's per-lock error with a generic one. The coordinator
        (``SlotEntityCoordinator._validate_credential_length``) is the
        authoritative minimum gate; an empty PIN is exempt because it clears
        the slot.
        """
        return self._DEFAULT_MIN

    @property
    @override
    def native_max(self) -> int:
        """
        Return the maximum value length -- always the permissive default.

        The advertised maximum is deliberately NOT surfaced here, for the same
        reason as the minimum. Home Assistant turns ``native_max`` into the
        field's ``maxlength``, so a lock advertising a limit lower than it
        really accepts would stop the keystrokes with no message at all: the
        field simply refuses to grow and nothing says why. The coordinator
        refuses the write instead, naming the lock and the range it claims,
        which is what somebody needs to see to recognise a bad advertisement.
        """
        return self._DEFAULT_MAX

    @property
    @override
    def native_value(self) -> str | None:
        """Return native value."""
        return self._state

    @override
    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        await TextEntity.async_added_to_hass(self)


class LockCodeManagerNameText(LockCodeManagerText):
    """A slot's name."""

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
    ) -> None:
        """Initialize the name entity."""
        super().__init__(
            hass, ent_reg, config_entry, slot_num, CONF_NAME, TextMode.TEXT
        )

    @override
    async def async_set_value(self, value: str) -> None:
        """Rename the slot's user."""
        coordinator = self._require_slot_coordinator()
        try:
            await coordinator.async_request_name_update(value)
        except InvalidNameError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=err.error_key,
                translation_placeholders=err.placeholders,
            ) from err
        self.async_write_ha_state()


class LockCodeManagerCredentialText(LockCodeManagerText):
    """
    One of a slot's credentials, keyed by its type.

    Sets no ``pattern``: Home Assistant's own refusal quotes the value
    (spec decision 6). The coordinator is the gate for every rule but one.
    Home Assistant caps a text entity at 255 characters whatever its
    ``native_max``, and ``text.set_value`` refuses a longer value before this
    entity sees it, with an error that quotes it. That is a limit of Home
    Assistant this integration cannot close.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
        credential_type: CredentialType,
    ) -> None:
        """Initialize the credential entity."""
        super().__init__(
            hass,
            ent_reg,
            config_entry,
            slot_num,
            credential_type.value,
            TextMode.PASSWORD,
        )
        self.credential_type = credential_type

    @override
    async def async_set_value(self, value: str) -> None:
        """Set or clear the credential."""
        await self._require_slot_coordinator().async_request_credential_update(
            self.credential_type, value
        )
        self.async_write_ha_state()
