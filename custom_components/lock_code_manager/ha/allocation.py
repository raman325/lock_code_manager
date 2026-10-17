"""
Finding slot numbers for users.

Nobody picks a slot number: callers name users and the numbers are
allocated around whatever the locks already hold. Keeping that in one
place is what stops the config flow and the services from disagreeing
about which numbers are free -- and only one of them reading the locks
would be the more dangerous disagreement.
"""

import asyncio
from collections.abc import Callable, Collection, Iterable, Sequence
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util.async_ import create_eager_task

from ..const import MAX_SEARCHED_SLOT
from ..model.credentials import CredentialType
from ..model.exceptions import LockCodeManagerError
from ..model.occupancy import LockOccupancy, Occupancy
from ..model.queries import slots_configured_on
from ..providers import resolve_provider_class_for_entity
from ..providers._base import BaseLock
from ..providers._util import foreign_slots
from ..sync.slot import Seen
from .locks import borrowed_lock_instance
from .runtime import lock_registry

_LOGGER = logging.getLogger(__name__)


class SlotAllocationError(LockCodeManagerError):
    """
    Numbers could not be issued, carrying the key that says why.

    The key and placeholders are the same ones the config flow renders as
    form errors and the services render as a failed action, so a refusal
    reads the same however it was reached.
    """

    def __init__(
        self, translation_key: str, placeholders: dict[str, Any] | None = None
    ) -> None:
        """Record the reason, in the form both surfaces can render."""
        super().__init__(translation_key)
        self.translation_key = translation_key
        self.placeholders: dict[str, Any] = placeholders or {}


class LockQuerySkipped(LockCodeManagerError):
    """
    Raised when no provider could be built for a lock.

    ``managed`` says whether Lock Code Manager will write credentials to the
    lock anyway. Only an unsupported platform means it will not. A lock
    missing from the entity registry, or whose integration's entry has gone,
    is one this entry still owns and merely could not reach -- reporting it
    as unmanaged would drop it from the occupancy check entirely and let
    allocation issue numbers against a lock it never read.
    """

    def __init__(self, lock_entity_id: str, *, managed: bool) -> None:
        """Record whether the lock is one credentials still get written to."""
        super().__init__(lock_entity_id)
        self.managed = managed


def build_lock_instance(
    hass: HomeAssistant,
    dev_reg: dr.DeviceRegistry,
    ent_reg: er.EntityRegistry,
    config_entry: ConfigEntry | None,
    lock_entity_id: str,
) -> Any:
    """
    Build a temporary lock provider instance for ``lock_entity_id``.

    Performs setup-time checks (entity in registry, a provider claims it,
    parent config entry exists) and instantiates the provider class.
    Raises ``LockQuerySkipped`` if any setup-time check fails.

    ``config_entry`` is the Lock Code Manager entry the lock is being read
    for, and is ``None`` only while the entry that will own it is still
    being created. What this factory decides about a lock -- above all
    whether an unbuildable one is still one credentials get written to --
    is a question about that entry's configuration, not about the lock
    alone, so the entry has to be reachable from here.
    """
    # Locks are shared between entries, so a skipped read names the entry
    # that asked as well as the lock it gave up on.
    asked_by = config_entry.entry_id if config_entry else "New entry"
    lock_entry = ent_reg.async_get(lock_entity_id)
    if not lock_entry:
        _LOGGER.warning(
            "%s: entity %s not found in registry; skipping usercode check",
            asked_by,
            lock_entity_id,
        )
        raise LockQuerySkipped(lock_entity_id, managed=True)
    lock_cls = resolve_provider_class_for_entity(dev_reg, lock_entry)
    if lock_cls is None:
        # Covers both an unsupported platform and an mqtt lock whose bridge
        # no provider speaks: either way nothing is ever written there, so
        # the lock constrains no numbering.
        _LOGGER.debug(
            "%s: no provider claims lock %s (platform %s); skipping usercode check",
            asked_by,
            lock_entity_id,
            lock_entry.platform,
        )
        raise LockQuerySkipped(lock_entity_id, managed=False)
    lock_config_entry = hass.config_entries.async_get_entry(lock_entry.config_entry_id)
    if lock_config_entry is None:
        _LOGGER.warning(
            "%s: config entry for lock %s not found; skipping usercode check",
            asked_by,
            lock_entity_id,
        )
        raise LockQuerySkipped(lock_entity_id, managed=True)

    return lock_cls(hass, dev_reg, ent_reg, lock_config_entry, lock_entry)


async def async_check_slot_capacity(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    locks: Iterable[str],
    slots_list: Iterable[int | str],
) -> None:
    """
    Reject slot numbers no lock in the set could ever hold.

    Only locks whose credential index IS the slot number are checked
    (``BaseLock.credential_index_follows_slot``) -- Matter allocates its own
    index, so a slot number above its credential count is legal there.
    Catching it here spares the user a write that fails forever on the device
    with nothing but a connectivity warning to show for it.

    A lock that cannot be queried is skipped rather than blocking the caller:
    capabilities need the lock awake, and a sleeping battery lock must not
    make the config flow unusable. The same check runs at write time, where
    it can suspend the affected slot precisely.

    ``config_entry`` is the entry the locks are being read for, on the terms
    ``build_lock_instance`` describes.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    slots = sorted({int(slot) for slot in slots_list})
    for lock_entity_id in locks:
        try:
            async with borrowed_lock_instance(
                build_lock_instance(
                    hass, dev_reg, ent_reg, config_entry, lock_entity_id
                )
            ) as lock_instance:
                if not lock_instance.credential_index_follows_slot:
                    continue
                capabilities = await lock_instance.async_get_capabilities()
        except LockQuerySkipped:
            continue
        except LockCodeManagerError as err:
            _LOGGER.debug(
                "Skipping slot capacity check for %s: %s", lock_entity_id, err
            )
            continue
        except Exception:
            _LOGGER.warning(
                "Skipping slot capacity check for %s; its slot count could not "
                "be determined",
                lock_entity_id,
                exc_info=True,
            )
            continue

        num_slots = capabilities.bounded_slot_count(CredentialType.PIN)
        if num_slots is None:
            continue
        if out_of_range := [slot for slot in slots if not 1 <= slot <= num_slots]:
            raise SlotAllocationError(
                "slot_out_of_range",
                {
                    "error_lock": lock_entity_id,
                    "num_slots": str(num_slots),
                    "out_of_range_slots": ", ".join(str(slot) for slot in out_of_range),
                },
            )


async def async_read_occupancy(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    locks: Sequence[str],
    indices: Collection[int],
    *,
    progress: Callable[[float], None] | None = None,
) -> Occupancy:
    """
    Ask every lock in ``locks`` which of ``indices`` it holds.

    A lock that cannot answer reports ``None``, never an empty set: that
    difference is what makes allocation refuse instead of issuing a
    number over a credential it never checked for. Every way of failing
    to answer has to arrive that way, including the ones no provider
    promised.

    ``config_entry`` is the entry being allocated for, which is also the
    one entry whose numbers do not constrain the answer: kept ones are held
    by tenure, released ones are free for whoever comes next. ``None`` is
    an entry still being created, which by the same rule holds nothing and
    so excludes nothing.

    ``progress`` is told, as a fraction, how many of the locks have answered,
    as each one does: a lock that answers one index per round trip can take a
    minute or more, and the flow showing this read should not look frozen
    while it waits. The answer keeps the order of ``locks`` however the reads
    finished, so an unreadable set is always named the same way.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    async def _read(lock_entity_id: str) -> LockOccupancy:
        try:
            lock_instance = build_lock_instance(
                hass, dev_reg, ent_reg, config_entry, lock_entity_id
            )
        except LockQuerySkipped as skipped:
            return LockOccupancy(
                lock_entity_id=lock_entity_id, managed=skipped.managed, occupied=None
            )
        except Exception:
            _LOGGER.warning(
                "Could not build a provider for %s; its contents are unknown",
                lock_entity_id,
                exc_info=True,
            )
            return LockOccupancy(
                lock_entity_id=lock_entity_id, managed=True, occupied=None
            )

        occupied: frozenset[int] | None = None
        async with borrowed_lock_instance(lock_instance):
            try:
                if not lock_instance.credential_index_follows_slot:
                    occupied = await _async_adoptable(hass, lock_instance, indices)
                else:
                    occupied = await _async_occupied(hass, lock_entity_id, indices)
                    if lock_instance.supports_native_users:
                        # The read gives a slot someone else's code at its
                        # index as empty (#1695): the slot's own view, not
                        # what the lock holds. Adopting it overwrites theirs.
                        occupied |= await _async_adoptable(hass, lock_instance, indices)
            except Exception:
                _LOGGER.warning(
                    "Failed to read the contents of %s; treating them as unknown",
                    lock_entity_id,
                    exc_info=True,
                )
        return LockOccupancy(
            lock_entity_id=lock_entity_id, managed=True, occupied=occupied
        )

    # Every lock at once, so the wait is the slowest lock rather than the sum
    # of them (#1536). Each lock reads in its own worker's turn, so locks do
    # not contend with one another; a lock named twice is read once, as two
    # reads of one lock would only queue behind each other. Started eagerly
    # and collected without yielding once all are in, so a read every lock
    # answers at once still finishes without a turn of the loop -- which is
    # what lets the flow skip its progress step (#1639).
    if progress is not None:
        progress(0.0 if locks else 1.0)
    reads = {lock: create_eager_task(_read(lock)) for lock in dict.fromkeys(locks)}
    pending = set(reads.values())
    try:
        while True:
            for read in [r for r in reads.values() if r in pending and r.done()]:
                pending.discard(read)
                read.result()
                if progress is not None:
                    progress((len(reads) - len(pending)) / len(reads))
            if not pending:
                break
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Reads are only left outstanding here when one raised past its own
        # handling or this read was cancelled: none is left running unowned.
        for read in pending:
            read.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    lock_occupancies = [reads[lock].result() for lock in locks]
    return Occupancy(
        locks=tuple(lock_occupancies),
        claimed_by_other_entries=frozenset(
            slot
            for lock_entity_id in locks
            for slot in slots_configured_on(
                hass, lock_entity_id, excluding=config_entry
            )
        ),
    )


def _too_far(
    num_users: int,
    max_slot: int,
    limiting_lock: str | None,
    needed: int | None = None,
) -> SlotAllocationError:
    """
    Explain that the numbers needed run past where the search may go.

    Two things decide the wording. Whose limit it is: a lock that
    reported its own range can be named and described as the lock's
    capacity, while a limit nothing reported must not be, or the user is
    sent to re-interview a lock over a number it never gave.

    And whether the count itself is too large, or only the numbers it
    would have to reach. ``needed`` names the number the last user would
    land on when existing codes have pushed them past the range -- a
    count that fits the lock told "N users will not fit" reads as a bug.
    """
    if limiting_lock is None:
        return SlotAllocationError(
            "search_limit_reached",
            {"num_users": str(num_users), "max_slot": str(max_slot)},
        )
    if needed is not None:
        return SlotAllocationError(
            "numbers_needed_exceed_capacity",
            {
                "num_users": str(num_users),
                "num_slots": str(max_slot),
                "needed": str(needed),
                "error_lock": limiting_lock,
            },
        )
    return SlotAllocationError(
        "too_many_users",
        {
            "num_users": str(num_users),
            "num_slots": str(max_slot),
            "error_lock": limiting_lock,
        },
    )


async def async_max_slot(
    hass: HomeAssistant, config_entry: ConfigEntry | None, locks: Sequence[str]
) -> tuple[int, str | None]:
    """
    Return how far a search for free numbers may go across these locks.

    The smallest answer wins, and comes back with the lock that gave it
    so a refusal can name the right one: a number past any single lock's
    range is a number that lock cannot hold, and every lock in an entry
    gets the same numbers. Locks that allocate their own credential index
    are not asked, because a slot number is not an index into them.

    A lock of ``None`` means nothing here could say and the limit is this
    integration's own -- which a message must not describe as a capacity
    some lock reported.

    ``config_entry`` is the entry the locks are being read for, on the terms
    ``build_lock_instance`` describes.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    limits: dict[str, int] = {}
    for lock_entity_id in locks:
        try:
            async with borrowed_lock_instance(
                build_lock_instance(
                    hass, dev_reg, ent_reg, config_entry, lock_entity_id
                )
            ) as lock_instance:
                if not lock_instance.credential_index_follows_slot:
                    continue
                # None is a lock with no opinion, not a lock with no slots.
                # Only a real answer earns a name, because the name is what
                # the refusal blames.
                if (bound := await lock_instance.async_get_max_slot()) is not None:
                    limits[lock_entity_id] = bound
        except Exception:
            _LOGGER.warning(
                "Could not ask %s how far its slot numbers go; "
                "searching only as far as this integration does",
                lock_entity_id,
                exc_info=True,
            )
    if not limits:
        return MAX_SEARCHED_SLOT, None
    # Ties are ordinary -- two locks of a kind answer alike -- so the
    # entity id breaks them, and the same lock is named every time.
    limiting = min(limits, key=lambda lock: (limits[lock], lock))
    return limits[limiting], limiting


async def async_allocate_for(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    locks: Sequence[str],
    num_users: int,
    *,
    held: Collection[int] = (),
    progress: Callable[[float], None] | None = None,
) -> frozenset[int]:
    """
    Find numbers for ``num_users``, reading only as far as it has to.

    Locks that answer one index per round trip make the width of this
    read its cost, so the window starts at the number of users and widens
    only by what turned out to be in the way, each pass asking only about
    numbers no earlier pass covered. Every index is read at most once.

    Terminating is not an accident: each pass either finds enough free
    numbers or discovers strictly MORE occupied ones than the pass
    before -- a pass discovering no more would have found the window big
    enough, the window being exactly the count plus what was in the way --
    and the locks hold finitely many credentials.

    Returns the numbers allocation must avoid, verified across a window
    wide enough to hold everyone. The users are numbered from it later,
    once they have names.

    ``config_entry`` is the entry the numbers are being allocated for, on the
    terms ``build_lock_instance`` describes. ``held`` names the numbers its
    users keep through this edit (``SlotAssignment.held_by``): taken by
    tenure, so they are not read -- reconcile keeps their users where they
    are, newcomers avoid them, and a lock answering one index per round trip
    is spared a read per existing user (issue #1536). A number the entry is
    releasing in the same edit is not held and is read like any other, so it
    is free for whoever comes next.

    Held numbers are not returned either. ``reconcile`` keeps them for the
    users holding them, so naming them again would only widen the window as
    if a stranger held them -- which refuses an entry that is merely full of
    its own users: ten users on a fifteen-slot lock could not take an
    eleventh, because the window would reach for twenty-one numbers to place
    eleven people.

    ``progress`` is passed to each read; see ``async_read_occupancy``.
    """
    try:
        await async_check_slot_capacity(hass, config_entry, locks, [num_users])
    except SlotAllocationError as err:
        raise SlotAllocationError(
            "too_many_users", {**err.placeholders, "num_users": str(num_users)}
        ) from err

    max_slot, limiting_lock = await async_max_slot(hass, config_entry, locks)
    if num_users > max_slot:
        # Before the first read, not just before each widening: a count
        # past the range walks off the end of the lock on the way in, and
        # a lock reading past-end as free would hand back all of it.
        raise _too_far(num_users, max_slot, limiting_lock)

    held = frozenset(held)
    unavailable: set[int] = set()
    # Each widening pass takes half of whatever fraction is left, so progress
    # only ever moves forward: how many passes there will be is not knowable
    # in advance, and a bar that restarts on each one reads as a hang -- the
    # symptom this progress exists to remove. Within a pass it is per lock, so
    # a single-lock entry sees the passes rather than the indices.
    reported = [0.0, 0.5]

    def _pass_progress(fraction: float) -> None:
        if progress is not None:
            progress(reported[0] + fraction * reported[1])

    read_up_to = 0
    window = num_users
    while True:
        # Only the part nobody has asked about yet; re-reading from one
        # each pass would cost a nearly-full lock several times its own
        # capacity to place a couple of users. Numbers this entry holds are
        # known taken and need no round trip.
        indices = [
            index for index in range(read_up_to + 1, window + 1) if index not in held
        ]
        if indices:
            occupancy = await async_read_occupancy(
                hass, config_entry, locks, indices, progress=_pass_progress
            )
            if not occupancy.is_known:
                # Unreadable is not free: issuing a number could overwrite a
                # credential programmed by hand on a lock that did not answer.
                raise SlotAllocationError(
                    "occupancy_unknown", {"locks": ", ".join(occupancy.unreadable)}
                )
            unavailable |= occupancy.unavailable
            reported[0] += reported[1]
            reported[1] /= 2
        read_up_to = window

        taken_in_window = sum(1 for slot in unavailable if slot <= window)
        if window - taken_in_window >= num_users:
            break

        # Every number in the way pushes the last user one further out.
        wider = num_users + taken_in_window
        try:
            await async_check_slot_capacity(hass, config_entry, locks, [wider])
        except SlotAllocationError as err:
            # Distinct from the count being too large: the count fits,
            # and the numbers needed to reach around what is already
            # there do not.
            raise SlotAllocationError(
                "numbers_needed_exceed_capacity",
                {
                    **err.placeholders,
                    "num_users": str(num_users),
                    "needed": str(wider),
                },
            ) from err
        if wider > max_slot:
            # Past the last number any of these locks holds. Searching on
            # would only read indices no lock has, and a lock cannot hand
            # back a slot it does not have -- every one of them would
            # come back occupied, forever.
            raise _too_far(num_users, max_slot, limiting_lock, needed=wider)
        window = wider

    if progress is not None:
        progress(1.0)

    # No capacity check here: every window this loop accepted was checked
    # before it was accepted -- the first as the bare count, each wider
    # one before widening to it -- and allocation only issues numbers
    # inside the window.
    return frozenset(unavailable)


async def _async_occupied(
    hass: HomeAssistant, lock_entity_id: str, indices: Collection[int]
) -> frozenset[int]:
    """
    Return which of ``indices`` the lock holds, read in its worker's turn.

    An index whose value could not be read still counts: it holds something,
    and over-reserving costs a user a slot number, while under-reserving
    costs them the code on their door. Silence says nothing, so it does not.
    """
    readings = await lock_registry(hass).read_once(lock_entity_id, indices)
    return frozenset(
        slot
        for slot, reading in readings.items()
        if reading is not Seen.EMPTY and reading is not Seen.SILENT
    )


async def _async_adoptable(
    hass: HomeAssistant, lock: BaseLock, indices: Collection[int]
) -> frozenset[int]:
    """
    Return which of ``indices`` a lock that picks its own credential index would adopt.

    Such a lock (Matter) is written by finding the user tagged for the slot,
    and failing that, adopting an UNTAGGED user holding a PIN at credential
    index == slot -- how Lock Code Manager 2.0 left its users. That user may
    just as well be one somebody made in the vendor's app, and adopting it
    renames them and writes over their PIN, so their index is occupied
    (``foreign_slots``). A tagged user is Lock Code Manager's own, held by
    whichever entry has the slot (``claimed_by_other_entries`` says which),
    and is not in the way; nor is an untagged one at its slot, as the write
    finds the tagged user first.
    """
    if (worker := lock_registry(hass).worker(lock.lock.entity_id)) is None:
        users = await lock.async_get_users()
    else:
        # In the lock's turn: an entry holding it may have a write in flight.
        users = await worker.turn(lock.async_get_users)
    return foreign_slots(users, lock.slot_tag) & frozenset(indices)
