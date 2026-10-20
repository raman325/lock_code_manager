"""Event entity for lock_code_manager."""

import logging
from typing import Any, override

from homeassistant.components.event import EventEntity
from homeassistant.const import ATTR_NAME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CREDENTIAL_TYPE,
    BUS_EVENT_CREDENTIAL_USED,
    EVENT_CREDENTIAL_USED,
)
from .entity import BaseLockCodeManagerEntity
from .ha.entry import LockCodeManagerConfigEntry
from .ha.runtime import signal_lock_changed
from .model.credentials import MANAGED_CREDENTIAL_TYPES
from .model.queries import get_entry_config, subentry_id_for_slot
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


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
            config_subentry_id=subentry_id_for_slot(config_entry, slot_num),
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

    @callback
    @override
    def _handle_add_locks(self, locks: list[BaseLock]) -> None:
        """Follow the added locks: what they can report widens the kinds."""
        super()._handle_add_locks(locks)
        self._watch_locks(locks)

    @callback
    def _watch_locks(self, locks: list[BaseLock]) -> None:
        """
        Write the state, kinds included, whenever one of ``locks`` reports a change.

        A lock's capabilities are probed lazily -- by its first write, say --
        and nothing else writes this entity's state until a use is recorded.
        """
        for lock in locks:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    signal_lock_changed(lock.lock.entity_id),
                    self.async_write_ha_state,
                )
            )

    @property
    @override
    def event_types(self) -> list[str]:
        """
        Return the credential kinds the entry's locks can report a use as.

        For each lock, the kinds its cached capabilities advertise and the
        kinds its provider declares in ``reported_credential_types``, which
        is how a provider that cannot ask the lock says what it reports. A
        lock with its own Radio Frequency Identification reader reports uses
        of a credential this integration never wrote, and they are still
        this user's uses.

        ``MANAGED_CREDENTIAL_TYPES`` is a floor under that union. A use
        reported through ``use_credential`` is one of those kinds whatever
        the locks are, and capabilities are probed lazily, so
        ``cached_capabilities`` reads ``None`` for a lock not asked yet or
        unreachable.

        Read fresh on every state write, because it moves: a probe
        completing or a lock being added changes it. Home Assistant re-reads
        capability attributes each write, so a dynamic answer publishes
        correctly; nothing caches it here, which is deliberate.
        """
        return sorted(
            MANAGED_CREDENTIAL_TYPES.union(
                *(lock.reported_credential_types for lock in self.locks),
                *(
                    caps.credential_types
                    for lock in self.locks
                    if (caps := lock.cached_capabilities) is not None
                ),
            )
        )

    @property
    @override
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

        A use of a kind ``event_types`` does not list is dropped, because
        ``_trigger_event`` raises on an undeclared type. That is a provider
        under-reporting what its lock can do, and the fix belongs in its
        capabilities. The use is not lost to everyone: the bus event
        (``lock_code_manager_credential_used``) has already fired, so an
        automation listening there still sees it.
        """
        credential_type = event.data[ATTR_CREDENTIAL_TYPE]
        if credential_type not in self.event_types:
            _LOGGER.debug(
                "%s: not recording a %s use, a kind none of its locks report",
                self.entity_id,
                credential_type,
            )
            return
        self._trigger_event(credential_type, event.data)
        self.async_write_ha_state()

    @override
    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        # EventEntity.async_added_to_hass restores __last_event_type from stored data
        await EventEntity.async_added_to_hass(self)
        self._watch_locks(self.locks)

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
