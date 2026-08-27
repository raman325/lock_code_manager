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
    ATTR_CREDENTIAL_TYPE,
    BUS_EVENT_CREDENTIAL_USED,
    EVENT_CREDENTIAL_USED,
)
from .domain.credentials import MANAGED_CREDENTIAL_TYPES, CredentialType
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

    The event type is the kind of credential that was presented, which is
    what Home Assistant's ``event_types`` is for: telling kinds of event
    apart. It is not where the use happened. A vocabulary of lock entity IDs
    was tried and is what this replaces -- Home Assistant refuses an event
    type an entity did not declare, so naming the entry's locks made a use
    against anything else impossible to record at all. Where the credential
    was used is the payload's ``target``, which nothing has to admit in
    advance.
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
        # Types this entity has actually recorded. A use is proof its kind is
        # possible here, so recording one widens the vocabulary rather than
        # being refused by it -- see :meth:`event_types`.
        self._recorded_types: set[CredentialType] = set()

    @property
    def event_types(self) -> list[str]:
        """
        Return the credential kinds a use recorded here can be.

        The union of what the entry's locks advertise and
        ``MANAGED_CREDENTIAL_TYPES``, which is a floor rather than a cap.
        Those two sets answer different questions and this entity spans
        both: the constant is what Lock Code Manager can WRITE, and a lock's
        advertised types are what it can REPORT. A lock with its own RFID
        reader reports uses of a credential this integration never wrote,
        and they are still this user's uses.

        The floor is what makes the answer safe before anything is known.
        Capabilities are probed lazily, so ``cached_capabilities`` reads
        ``None`` for a lock that has not been asked yet or cannot be
        reached, and an empty vocabulary would make ``_trigger_event``
        refuse everything -- the entity would record nothing at all, for as
        long as the probe took.

        Read fresh on every state write, because it moves: a probe
        completing or a lock being added changes it. Home Assistant re-reads
        capability attributes each write, so a dynamic answer publishes
        correctly; nothing caches it here, which is deliberate.
        """
        advertised = {
            credential_type
            for lock in self.locks
            if (caps := lock.cached_capabilities) is not None
            for credential_type in caps.credential_types
        }
        return sorted(advertised | MANAGED_CREDENTIAL_TYPES | self._recorded_types)

    @property
    def available(self) -> bool:
        """
        Return True whenever the entry is loaded.

        Deliberately not the shared per-slot rule, which asks whether any of
        the entry's locks is reachable. A use recorded here need not have
        come from a lock at all: ``use_credential`` carries uses this
        integration cannot observe, and refuses nothing when every lock is
        unreachable -- which is a likely reason to be using it.

        Gating on lock reachability hid those uses twice. The entity read
        ``unavailable`` while holding the use, and when a lock recovered the
        use surfaced as an ``unavailable -> <timestamp>`` transition, which
        consumers discard on purpose as lock recovery rather than a use.

        What a lock being down actually means is reported by that lock's own
        entity and by the slot's in-sync sensor, neither of which this
        answer speaks for.
        """
        return True

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
        # Every entry's entities see every entry's events, and resolving who
        # holds this slot walks the configuration, so the entry has to be
        # ruled out before that lookup rather than alongside it.
        if event_data[ATTR_CONFIG_ENTRY_ID] != self.entry_id:
            return False
        return event_data[ATTR_NAME] == get_entry_config(self.config_entry).name_for(
            self.slot_num
        )

    @callback
    def _handle_credential_used(self, event: Event) -> None:
        """
        Record the use, publishing the unified payload as state attributes.

        The whole payload, unedited: a consumer reads ``target`` for where
        the credential was used and ``source`` for where it was entered,
        which for a use a lock observed itself are the same entity.

        A kind the vocabulary does not yet list widens it instead of being
        dropped. ``_trigger_event`` raises on an undeclared type, and this
        runs on a bus callback whose exceptions Home Assistant swallows, so
        refusing would lose the use in silence -- and a use that arrived is
        better evidence of what is possible here than a capability probe
        that has not finished.
        """
        credential_type = event.data[ATTR_CREDENTIAL_TYPE]
        self._recorded_types.add(credential_type)
        self._trigger_event(credential_type, event.data)
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
