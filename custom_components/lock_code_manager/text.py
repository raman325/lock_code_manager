"""Text for lock_code_manager."""

import logging
from typing import override

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import BaseLockCodeManagerEntity
from .ha.entry import LockCodeManagerConfigEntry, available_credential_types
from .ha.slot_coordinator import InvalidNameError
from .model.credentials import CredentialType
from .model.queries import get_entry_config, subentry_id_for_slot
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""
    runtime_data = config_entry.runtime_data
    callbacks = runtime_data.callbacks
    # Slots with a password entity now: the type follows the entry's locks.
    with_password: set[int] = set()

    @callback
    def add_password_entity(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        if slot_num in with_password:
            return
        with_password.add(slot_num)
        async_add_entities(
            [
                LockCodeManagerCredentialText(
                    hass, ent_reg, config_entry, slot_num, CredentialType.PASSWORD
                )
            ],
            True,
            config_subentry_id=subentry_id_for_slot(config_entry, slot_num),
        )

    @callback
    def add_standard_text_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add a slot's name and PIN, and its password if the entry offers one."""
        # A number issued again: its earlier password entity left with the
        # slot that held it.
        with_password.discard(slot_num)
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
        if CredentialType.PASSWORD in available_credential_types(config_entry):
            add_password_entity(slot_num, ent_reg)

    @callback
    def locks_added(locks: list[BaseLock]) -> None:
        if not any(
            CredentialType.PASSWORD in lock.managed_credential_types for lock in locks
        ):
            return
        ent_reg = er.async_get(hass)
        for slot_num in get_entry_config(config_entry).slot_numbers:
            add_password_entity(slot_num, ent_reg)

    @callback
    def lock_removed(lock_entity_id: str) -> None:
        # The removed lock is still held while its handlers run.
        remaining = (
            lock
            for entity_id, lock in runtime_data.locks.items()
            if entity_id != lock_entity_id
        )
        if any(
            CredentialType.PASSWORD in lock.managed_credential_types
            for lock in remaining
        ):
            return
        for slot_num in sorted(with_password):
            with_password.discard(slot_num)
            config_entry.async_create_task(
                hass,
                callbacks.async_remove_entity(f"{slot_num}|password"),
                f"Remove slot {slot_num} password entity",
            )

    config_entry.async_on_unload(
        callbacks.register_standard_adder(add_standard_text_entities)
    )
    config_entry.async_on_unload(callbacks.register_lock_added_handler(locks_added))
    config_entry.async_on_unload(callbacks.register_lock_removed_handler(lock_removed))
    return True


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
    (spec decision 6). The coordinator is the only gate.
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
