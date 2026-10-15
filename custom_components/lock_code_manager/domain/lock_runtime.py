"""
The core, wired into Home Assistant: one lock registry per instance.

Each lock gets its provider, a ``ProviderPort`` around it and the
``LockWorker`` that owns every exchange with it, built when an entry first
holds the lock. The provider is set up before the worker first reads it and
unloaded after the worker stops. A worker's changes go out as one dispatcher
signal per lock; entities and the websocket listen to it.

The design: docs/superpowers/specs/2026-10-14-core-swap.md.
"""

import asyncio
from collections.abc import Iterable, Mapping
import logging
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.start import async_at_started

from ..const import DATA, DOMAIN, MAX_SEARCHED_SLOT, LockCodeManagerData
from ..core.adapter import ProviderPort, lock_error
from ..core.registry import LockRegistry, SlotTaken
from ..core.slot import Holds, Reading, Repair, Seen, Status, Want
from ..core.worker import LockWorker
from ..providers import BaseLock
from .exceptions import LockCodeManagerProviderError
from .lock_store import LockStore
from .locks import async_create_lock_instance
from .models import SlotCode
from .queries import (
    configured_owner,
    entries_configuring,
    every_configured_lock,
    get_entry_config,
    iter_loaded_lcm_entries,
)
from .util import per_lock_issue_id

_LOGGER = logging.getLogger(__name__)

REGISTRY_KEY: Final = "lock_registry"
# Locks leaving Lock Code Manager altogether: their provider's kept state goes.
LEAVING_KEY: Final = "locks_leaving"
# (lock, slot) -> the entry that gave the slot up, and the worker clearing it:
# its repair is that entry's, and only that worker's view of it resolves it.
GIVEN_UP_KEY: Final = "slots_given_up"
# lock -> the per-slot issues raised for it, so a resolved one is deleted.
ISSUES_KEY: Final = "slot_issues"
# What the workers remember across a restart.
STORE_KEY: Final = "lock_store"
# lock -> whether a read has described a slot of it, for every lock read this
# run (``ProviderPort.read``). In memory only: a restart starts it over.
REPORTS_KEY: Final = "locks_reporting"
# Everything the core keeps for one run of Home Assistant.
CORE_KEYS = (
    REGISTRY_KEY,
    LEAVING_KEY,
    GIVEN_UP_KEY,
    ISSUES_KEY,
    STORE_KEY,
    REPORTS_KEY,
)

# How long a one-off write from a repair or the card is waited for.
WRITE_ONCE_WAIT = 60.0

_HOLD_ISSUES = {
    Repair.CANT_SET: (
        "slot_suspended",
        "the lock did not take the code after several tries",
    ),
    Repair.CODE_MAY_REMAIN: (
        "slot_clear_failed",
        "the lock did not clear the slot after two tries",
    ),
}


def signal_lock_changed(lock_entity_id: str) -> str:
    """Return the dispatcher signal for a change on ``lock_entity_id``."""
    return f"{DOMAIN}_lock_changed_{lock_entity_id}"


@callback
def lock_registry(hass: HomeAssistant) -> LockRegistry[ProviderPort]:
    """
    Return the instance's lock registry, creating it on first use.

    A config flow can read a lock before the integration has set up.
    """
    data = hass.data.setdefault(DATA, LockCodeManagerData(resources=False))
    if REGISTRY_KEY not in data:
        return async_setup_lock_registry(hass)
    return data[REGISTRY_KEY]


async def async_setup_core(hass: HomeAssistant) -> None:
    """
    Start the core: what the workers remember, then the lock registry.

    Before any entry sets up, so the first worker starts from it. A lock no
    entry lists any more -- a disabled entry's included -- or no longer in
    the entity registry is forgotten, with any clear still owed on it: there
    is nothing to set its provider up with. The clears still owed elsewhere go on once Home Assistant has
    started, every entry loaded; a slot an entry has claimed meanwhile is
    that entry's now, and is only read again.
    """
    data = hass.data.setdefault(DATA, LockCodeManagerData(resources=False))
    store = data[STORE_KEY] = LockStore(hass)
    await store.async_load()
    for record in store.prune(every_configured_lock(hass)):
        lock = record["entity_id"]
        async_delete_issue(hass, DOMAIN, per_lock_issue_id("lock_offline", lock))
        _warn_not_cleared(
            lock, (int(n) for n, saved in record["slots"].items() if "release" in saved)
        )
    lock_registry(hass)

    async def _owed(hass: HomeAssistant) -> None:
        for lock, slot, entry_id in store.releases():
            await async_resume_slot(hass, lock, slot, entry_id=entry_id, release=True)

    async_at_started(hass, _owed)


def _warn_not_cleared(lock: str, slots: Iterable[int]) -> None:
    """Say that clears owed on a lock leaving Lock Code Manager will not be sent."""
    for slot in slots:
        _LOGGER.warning(
            "Slot %s on %s was still being cleared when the lock left Lock "
            "Code Manager: it is not cleared, and any code left in it stays "
            "on the lock",
            slot,
            lock,
        )


@callback
def async_setup_lock_registry(hass: HomeAssistant) -> LockRegistry[ProviderPort]:
    """Create the instance's lock registry; it stops when Home Assistant does."""
    data = hass.data.setdefault(DATA, LockCodeManagerData(resources=False))
    data[LEAVING_KEY] = set()
    data[GIVEN_UP_KEY] = {}
    data[ISSUES_KEY] = {}
    data[REPORTS_KEY] = {}

    def _make(lock_entity_id: str) -> LockWorker[ProviderPort]:
        # A config flow's read before the integration has set up remembers nothing.
        store: LockStore | None = data.get(STORE_KEY)
        provider = async_create_lock_instance(
            hass, dr.async_get(hass), er.async_get(hass), None, lock_entity_id
        )

        def _name_for(slot: int) -> str | None:
            if (entry := configured_owner(hass, lock_entity_id, slot)) is None:
                return None
            return get_entry_config(entry).slots.get(slot, {}).get(CONF_NAME)

        @callback
        def _on_change() -> None:
            # A release done (or replaced) is over: a later clear of the slot
            # is a plain one, not another release.
            port.releasing.intersection_update(
                n for n, slot in worker.slots.items() if slot.release
            )
            async_reconcile_issues(hass, lock_entity_id, worker)
            _remember(lock_entity_id, worker)
            async_dispatcher_send(hass, signal_lock_changed(lock_entity_id))

        @callback
        def _on_disable(slot: int) -> None:
            if (entry := configured_owner(hass, lock_entity_id, slot)) is None:
                return
            # Why, as the lock or the duplicate check said it (which slot
            # already has the PIN): re-enabling blind is refused again. Read
            # first: turning the slot off (an eager task) starts it over.
            held = worker.slots.get(slot)
            why = f" {held.why}" if held is not None and held.why else ""
            # Through the config, not the switch: during setup the switch
            # entity may not exist yet, and it reads the config when it does.
            runtime = getattr(entry, "runtime_data", None)
            coordinator = runtime.slot_coordinators.get(slot) if runtime else None
            if coordinator is not None:
                hass.async_create_task(
                    coordinator.async_request_active_toggle(False),
                    f"Disable slot {slot} refused by {lock_entity_id}",
                )
            async_create_issue(
                hass,
                DOMAIN,
                f"slot_disabled_{entry.entry_id}_{slot}",
                is_fixable=True,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="slot_disabled",
                translation_placeholders={
                    "slot_num": str(slot),
                    "reason": (
                        f"Lock **{lock_entity_id}** refused the PIN for slot "
                        f"**{slot}**.{why}\n\nFix the issue and re-enable the slot."
                    ),
                },
            )

        def _polling() -> bool:
            # Polled unless every entry that lists the lock has disabled it.
            return any(
                not entry.pref_disable_polling
                for entry, _ in entries_configuring(hass, lock_entity_id)
            )

        port = ProviderPort(
            provider, name_for=_name_for, polling=_polling, reports=data[REPORTS_KEY]
        )
        worker = LockWorker(
            port,
            # What a write is remembered by. Before the integration has set up
            # (a config flow's read) there is no instance id, and nothing is
            # written then that outlives the worker.
            str(data.get("instance_id", "")).encode(),
            # The event loop's clock, which its sleeps keep to.
            clock=hass.loop.time,
            # The pause the lock needs between exchanges, as its provider says.
            gap=provider._min_operation_delay,
            # A backstop only: the port bounds each read by the walk it makes,
            # so this fits the longest walk anything here asks for.
            budget=provider._operation_budget(MAX_SEARCHED_SLOT),
            on_change=_on_change,
            on_send=lambda: _remember(lock_entity_id, worker),
            on_disable=_on_disable,
            recall=lambda slot: store.wrote(lock_entity_id, slot) if store else None,
            answered_before=store is not None and store.answered(lock_entity_id),
        )
        port.worker = worker
        return worker

    async def _prepare(lock_entity_id: str, worker: LockWorker[ProviderPort]) -> None:
        # The provider keeps an entry for re-running its setup on reconnect;
        # any entry that lists the lock serves.
        entry = next((e for e, _ in entries_configuring(hass, lock_entity_id)), None)
        if entry is None:
            # A clear given up by an entry that has since let go of the lock
            # (decision F): its provider still has to be set up to send it.
            entry = next(
                (
                    found
                    for (lock, _), (entry_id, _) in data[GIVEN_UP_KEY].items()
                    if lock == lock_entity_id
                    and (found := hass.config_entries.async_get_entry(entry_id))
                ),
                None,
            )
        if entry is not None:
            await worker.port.async_setup(entry)

    @callback
    def _remember(lock_entity_id: str, worker: LockWorker[ProviderPort]) -> None:
        if (store := data.get(STORE_KEY)) is not None:
            store.remember(
                lock_entity_id,
                worker,
                lambda slot: data[GIVEN_UP_KEY].get(
                    (lock_entity_id, slot), (None, None)
                )[0],
            )

    async def _cleanup(lock_entity_id: str, worker: LockWorker[ProviderPort]) -> None:
        port = worker.port
        leaving = lock_entity_id in data[LEAVING_KEY]
        if leaving:
            # Nothing will answer it now: the lock is gone from here.
            async_delete_issue(
                hass, DOMAIN, per_lock_issue_id("lock_offline", lock_entity_id)
            )
            if (store := data.get(STORE_KEY)) is not None:
                store.forget_lock(lock_entity_id)
            _warn_not_cleared(
                lock_entity_id,
                (
                    n
                    for n, slot in worker.slots.items()
                    if slot.release and slot.hold is None
                ),
            )
        if port.managed:
            await port.async_unload(leaving)
        else:
            # A one-off read's provider: drop what reading subscribed to, and
            # nothing else (unloading the virtual lock writes its store back).
            port.provider.unsubscribe_push_updates()
        data[LEAVING_KEY].discard(lock_entity_id)

    registry = data[REGISTRY_KEY] = LockRegistry(
        _make, prepare=_prepare, cleanup=_cleanup
    )

    async def _stop(event: Event) -> None:
        await registry.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    return registry


async def async_hold_lock(
    hass: HomeAssistant,
    entry: ConfigEntry,
    lock_entity_id: str,
    wants: Mapping[int, Want],
) -> BaseLock:
    """Hold ``lock_entity_id`` for ``entry`` with its slots' wants; return the provider."""
    wants = dict(wants)
    while True:
        try:
            worker = await lock_registry(hass).hold(
                lock_entity_id, entry.entry_id, wants
            )
            break
        except SlotTaken as err:
            # Another entry already manages that slot on this lock: it stays
            # theirs, and this entry still gets the lock for its other slots.
            _LOGGER.warning(
                "%s: slot %s on %s is managed by another entry (%s); "
                "leaving it to that entry",
                entry.entry_id,
                err.slot,
                lock_entity_id,
                err.owner,
            )
            del wants[err.slot]
    port = worker.port
    port.releasing.difference_update(wants)
    return port.provider


async def async_release_lock(
    hass: HomeAssistant, entry: ConfigEntry, lock_entity_id: str, *, leaving: bool
) -> None:
    """
    Let go of ``lock_entity_id`` for ``entry``, leaving its codes on the lock.

    ``leaving`` when the lock leaves Lock Code Manager altogether: the
    provider's kept state goes with it once no entry holds it.
    """
    if leaving:
        hass.data[DATA][LEAVING_KEY].add(lock_entity_id)
    await lock_registry(hass).release(lock_entity_id, entry.entry_id)


@callback
def async_hand_off(
    hass: HomeAssistant, leaving: ConfigEntry, lock_entity_id: str, slots: Iterable[int]
) -> None:
    """
    Hand ``slots``, which ``leaving`` let go of, to another entry configuring them.

    Only old storage has two entries on one slot (the config flow forbids
    it): the second one's claim was dropped when it held the lock, so once
    the owner lets the slot go, nobody would want it again. Telling the
    other entry what it wants claims the slot; for a slot it already owns,
    that is nothing new.

    Only for an owner that stops configuring the slot: given up, or its
    entry deleted. One that merely unloads -- a reload, or disabled --
    still configures it, and takes it back when it loads, unless the other
    entry claimed the slot meanwhile by telling the lock what it wants.
    """
    for slot in slots:
        for entry in iter_loaded_lcm_entries(hass):
            runtime = entry.runtime_data
            if (
                entry.entry_id != leaving.entry_id
                and lock_entity_id in runtime.locks
                and (coordinator := runtime.slot_coordinators.get(slot))
            ):
                coordinator.async_push_want([lock_entity_id])
                break


@callback
def async_want(
    hass: HomeAssistant, entry: ConfigEntry, lock_entity_id: str, slot: int, pin: Want
) -> None:
    """Set what ``slot`` should hold on a lock ``entry`` holds."""
    registry = lock_registry(hass)
    if (worker := registry.worker(lock_entity_id)) is None:
        return
    port = worker.port
    current = worker.slots.get(slot)
    if (
        current is not None
        and current.want == pin
        and not current.release
        and slot not in port.releasing
    ):
        return  # nothing new to tell the lock: no sync to queue
    port.releasing.discard(slot)
    hass.data[DATA][GIVEN_UP_KEY].pop((lock_entity_id, slot), None)
    try:
        registry.want(lock_entity_id, entry.entry_id, slot, pin)
    except (KeyError, SlotTaken) as err:
        _LOGGER.error(
            "%s: cannot set slot %s on %s: %s",
            entry.entry_id,
            slot,
            lock_entity_id,
            err,
        )
        return
    # A sync is queued: views show it syncing now, not once a job finishes.
    async_dispatcher_send(hass, signal_lock_changed(lock_entity_id))


@callback
def async_give_up(
    hass: HomeAssistant,
    entry: ConfigEntry,
    lock_entity_id: str,
    slot: int,
    *,
    clear: bool = True,
) -> None:
    """
    Stop managing ``slot`` on a lock ``entry`` holds.

    Cleared on the way out unless ``clear`` is off (a user handed off with
    their code left programmed): a release, which goes on after the entry
    unloads.
    """
    registry = lock_registry(hass)
    if (worker := registry.worker(lock_entity_id)) is None:
        return
    port = worker.port
    if clear and not worker.answered:
        # A lock with no PIN support holds no codes, unless it answered an
        # earlier run that wrote some: no clear to wait on. Every other
        # failed setup (a node dead at boot, a slot count not known until a
        # re-interview, a retry under way) may hold codes, and the clear waits
        # for it to recover -- except one that crashed, which nothing retries
        # short of a reload: that clear would wait forever.
        if port.provider.pin_unsupported and not worker.answered_ever:
            clear = False
        elif port.setup_crashed:
            _LOGGER.warning(
                "Slot %s on %s was given up without clearing it: the lock's "
                "setup failed with an unexpected error, so nothing can be sent "
                "to it. Any code left in that slot stays on the lock",
                slot,
                lock_entity_id,
            )
            clear = False
    try:
        if clear:
            port.releasing.add(slot)
            registry.give_up(lock_entity_id, entry.entry_id, slot)
            hass.data[DATA][GIVEN_UP_KEY][lock_entity_id, slot] = (
                entry.entry_id,
                worker,
            )
        else:
            registry.unclaim(lock_entity_id, entry.entry_id, slot)
            if (store := hass.data[DATA].get(STORE_KEY)) is not None:
                store.forget_slot(lock_entity_id, slot)
    except KeyError:
        return  # never held: nothing to give up
    async_hand_off(hass, entry, lock_entity_id, [slot])


async def async_write_once(
    hass: HomeAssistant, lock_entity_id: str, slot: int, pin: Want
) -> bool | None:
    """
    Put ``pin`` in a slot no entry owns, or clear it for None, and wait.

    True once it is done, False once the lock has refused or the slot is
    held after its tries; None if it is still going when the wait runs out
    (the lock is backing off), and it carries on.
    """
    worker = await lock_registry(hass).write_once(lock_entity_id, slot, pin)
    done = asyncio.Event()

    @callback
    def _check() -> None:
        if slot not in worker.slots or worker.slots[slot].hold is not None:
            done.set()

    unsub = async_dispatcher_connect(hass, signal_lock_changed(lock_entity_id), _check)
    try:
        _check()
        async with asyncio.timeout(WRITE_ONCE_WAIT):
            await done.wait()
    except TimeoutError:
        return None
    finally:
        unsub()
    return slot not in worker.slots


@callback
def async_reconcile_issues(
    hass: HomeAssistant, lock_entity_id: str, worker: LockWorker[ProviderPort]
) -> None:
    """
    Raise a repair for each slot the worker holds, and drop resolved ones.

    A slot it could not set is ``slot_suspended``; a clear that would not
    take is ``slot_clear_failed`` -- the code may still open the door. A
    refused PIN turns the slot off through ``on_disable`` instead. A lock
    that answered before and has stopped answering is ``lock_offline``.
    """
    data = hass.data[DATA]
    given_up = data[GIVEN_UP_KEY]
    for key, (_, clearing) in list(given_up.items()):
        if (
            key[0] == lock_entity_id
            and clearing is worker
            and key[1] not in worker.slots
        ):
            del given_up[key]  # cleared and dropped: nothing left to resume
            if (store := data.get(STORE_KEY)) is not None:
                store.forget_slot(*key)
    # A clear held by a worker since stopped (decision F) is not this
    # worker's to resolve: its repair stays until it is tried again.
    kept = {
        f"slot_suspended_{entry_id}_{lock_entity_id}_{slot_num}"
        for (lock, slot_num), (entry_id, clearing) in given_up.items()
        if lock == lock_entity_id and clearing is not worker
    }
    raised: set[str] = set()
    for slot_num, slot in worker.slots.items():
        owner = configured_owner(hass, lock_entity_id, slot_num)
        if (
            owner is not None
            and slot.want is not None
            and worker.status(slot_num)[0] is Status.IN_SYNC
        ):
            # Re-enabled and on the lock: a refusal's repair is answered.
            async_delete_issue(
                hass, DOMAIN, f"slot_disabled_{owner.entry_id}_{slot_num}"
            )
        if owner is not None and worker.status(slot_num)[0] is Status.IN_SYNC:
            # In sync: a hold's repair is answered, even one a previous run
            # raised (it is persistent).
            async_delete_issue(
                hass,
                DOMAIN,
                f"slot_suspended_{owner.entry_id}_{lock_entity_id}_{slot_num}",
            )
        if (issue := _HOLD_ISSUES.get(slot.hold)) is None:  # type: ignore[arg-type]
            continue
        entry_id = (
            owner.entry_id
            if owner is not None
            else given_up.get((lock_entity_id, slot_num), (None, None))[0]
        )
        if entry_id is None:
            continue
        issue_id = f"slot_suspended_{entry_id}_{lock_entity_id}_{slot_num}"
        raised.add(issue_id)
        translation_key, reason = issue
        provider = worker.port.provider
        # A cause the provider named, alone: the link's counters would point
        # away from it. Otherwise the driver's own words over ours when it
        # would not set the code (#1398) -- a refused clear's are about
        # setting, and mislead -- then whatever the provider can measure of
        # its transport, verbatim (#1397).
        if slot.hold is Repair.CANT_SET and slot.cause:
            reason = slot.cause
        else:
            if slot.hold is Repair.CANT_SET and slot.why:
                reason = slot.why
            if link_health := provider.describe_link_health():
                reason = f"{reason}\n\n{link_health}"
        async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={
                "lock_entity_id": lock_entity_id,
                "lock_name": provider.display_name or lock_entity_id,
                "slot_num": str(slot_num),
                "reason": reason,
            },
            # Persisted with the repair: after a restart, acknowledging it
            # still knows whose slot it was and that it was on its way out.
            data={
                "lock_entity_id": lock_entity_id,
                "slot": slot_num,
                "entry_id": entry_id,
                "release": slot.release,
            },
        )
    for issue_id in data[ISSUES_KEY].get(lock_entity_id, set()) - raised - kept:
        async_delete_issue(hass, DOMAIN, issue_id)
    data[ISSUES_KEY][lock_entity_id] = raised | kept
    offline_issue = per_lock_issue_id("lock_offline", lock_entity_id)
    if worker.offline:
        async_create_issue(
            hass,
            DOMAIN,
            offline_issue,
            is_fixable=False,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key="lock_offline",
            translation_placeholders={"lock_entity_id": lock_entity_id},
        )
    elif worker.answered and worker.reachable:
        # Only the lock answering resolves it: a worker started since (a
        # reload, a restart) has not heard from a lock that is still gone.
        async_delete_issue(hass, DOMAIN, offline_issue)


async def async_resume_slot(
    hass: HomeAssistant,
    lock_entity_id: str,
    slot: int,
    *,
    entry_id: str | None = None,
    release: bool = False,
) -> None:
    """
    Try a held slot again: the user acknowledged its repair.

    A clear given up on a lock no entry holds any more has no worker left;
    it is started over as a one-off clear -- a release still. ``entry_id``
    and ``release`` come from the repair, which outlives a restart where
    what was remembered of the slot does not.
    """
    worker = lock_registry(hass).worker(lock_entity_id)
    if worker is not None and slot in worker.slots:
        worker.resume(slot)
        return
    given_up = hass.data[DATA][GIVEN_UP_KEY]
    key = (lock_entity_id, slot)
    if key not in given_up and release and entry_id is not None:
        given_up[key] = (entry_id, None)
    if key in given_up:
        worker = await lock_registry(hass).write_once(
            lock_entity_id,
            slot,
            None,
            # Still a release: a native-user lock deletes the user it tagged.
            before=lambda new: new.port.releasing.add(slot),
        )
        # The clear is this worker's now: its view of the slot resolves it.
        given_up[key] = (given_up[key][0], worker)


def _label(reading: Reading) -> str | SlotCode:
    """Return a reading as the views are told it: its PIN, or what the lock said."""
    if isinstance(reading, Holds):
        return reading.pin
    return {
        Seen.EMPTY: SlotCode.EMPTY,
        Seen.MASKED: SlotCode.UNREADABLE_CODE,
        Seen.SILENT: SlotCode.UNANSWERED,
        Seen.DISABLED: SlotCode.DISABLED,
    }[reading]


@callback
def lock_codes(hass: HomeAssistant, lock_entity_id: str) -> dict[int, str | SlotCode]:
    """
    Return every code the lock has told of: managed slots and the rest.

    What the lock said, never the PIN a slot wants: a card stands the
    configured PIN in for a slot in sync itself, marked as configured rather
    than read (spec D, #1282).

    A managed slot without a reading is never shown by an older word in
    ``port.others``: that may be the very reading set aside to read the slot
    again (``Slot.doubt``). On a lock that has answered it is being checked,
    as its per-lock entities say; before then it is left out.
    """
    if (worker := lock_registry(hass).worker(lock_entity_id)) is None:
        return {}
    not_reported = worker.port.not_reported

    def _label_of(slot: int, reading: Reading) -> str | SlotCode:
        if slot in not_reported and reading in (Seen.SILENT, Seen.MASKED):
            return SlotCode.NOT_REPORTED
        return _label(reading)

    codes = {
        slot: _label_of(slot, reading) for slot, reading in worker.port.others.items()
    }
    for slot_num, slot in worker.slots.items():
        if slot.seen is not None:
            codes[slot_num] = _label_of(slot_num, slot.seen)
        elif not slot.release:
            if worker.answered:
                codes[slot_num] = SlotCode.CHECKING
            else:
                codes.pop(slot_num, None)
    return codes


async def async_hard_refresh(worker: LockWorker[ProviderPort]) -> None:
    """
    Re-read the whole lock, past any cache, in its turn: the hard refresh service.

    The probe brings a lock that is backing off back; the provider's unscoped
    hard refresh re-reads every slot (codes nobody here manages included);
    the read after it brings what it found into view.
    """
    await worker.read(probe=True)
    provider = worker.port.provider

    async def _walk() -> None:
        try:
            await provider.async_hard_refresh_codes()
        except LockCodeManagerProviderError as err:
            if (error := lock_error(err, clearing=False)) is None:
                raise
            raise error from err

    await worker.turn(_walk)
    await worker.read()


@callback
def lock_reachable(hass: HomeAssistant, lock_entity_id: str) -> bool | None:
    """Return whether the lock is answering; None without a worker."""
    worker = lock_registry(hass).worker(lock_entity_id)
    return None if worker is None else worker.reachable


@callback
def locks_that_cannot_report(hass: HomeAssistant, locks: Iterable[str]) -> list[str]:
    """
    Return those of ``locks`` read this run that never described a slot (#1581).

    Every slot read came back silent or cannot-tell, so a code set at the
    keypad is invisible, and the slots handed out may hold one.
    """
    reports = hass.data.get(DATA, {}).get(REPORTS_KEY, {})
    return [lock for lock in locks if reports.get(lock) is False]
