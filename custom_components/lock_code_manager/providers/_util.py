"""
Shared helpers for provider implementations.

Provider-internal utilities that don't belong in the integration-wide
``util.py``: withheld-code recognition, and MQTT discovery lookups shared
by the providers that reach their locks through an MQTT bridge, plus the
slot-tagging used by providers whose lock APIs identify codes by user-name
rather than by slot number (Schlage, Akuvox, and -- once the unified-user
migration lands -- Matter and Z-Wave User Credential CC).

Slot tag formats
----------------

Three formats are emitted today; a fourth (legacy) is still tolerated on
read for locks whose names were written by older releases.

* Canonical ``lcm:<slot>:<name>`` -- the consolidated format every new
  write uses when the lock's ``max_user_name_length`` can fit the full
  ``lcm:<slot>:`` prefix. Two colons delimit the slot field clearly for
  visual parseability on the lock UI.
* Compact ``lcm<slot>`` -- the charset-constrained fallback, used when
  the lock's firmware rejects the colons (Matter spec is permissive but
  many firmwares restrict ``userName`` to alphanumeric). Drops the
  display portion but keeps the LCM ownership marker, so the lock-side
  name is unambiguous about who owns the user record. Strictly better
  than the slot-only fallback for coexistence with other controllers,
  which is why providers try this tier first before falling all the way
  back to ``str(slot)``.
* Slot-only ``<digits>`` -- the deepest fallback, used when neither
  canonical nor compact fit. Just the slot number, written as
  ``str(slot)``. Ambiguous with any external user named with only
  digits, but only encountered on locks with absurdly small name limits
  or firmwares that reject even ``lcm<slot>``, so the tradeoff is
  accepted.
* Legacy ``[LCM:<slot>] <name>`` -- read-only. Older Schlage/Akuvox
  releases wrote this format; ``parse_tag`` still recognizes it so the
  integration can identify its own users on locks that haven't been
  rewritten yet. The next write naturally replaces the lock-stored name
  with the canonical format.
"""

from collections.abc import Iterable
import re
from typing import Any

from homeassistant.components.mqtt import debug_info as mqtt_debug_info
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..model.credentials import User
from .const import LOGGER

_LEGACY_SLOT_TAG_RE = re.compile(r"^\[LCM:(\d+)\]\s*(.*)")
_TAG_RE = re.compile(r"^lcm:(\d+):\s*(.*)")
_COMPACT_TAG_RE = re.compile(r"^lcm(\d+)$")
_SLOT_ONLY_RE = re.compile(r"^(\d+)$")


def make_tagged_name(slot_num: int, name: str | None = None) -> str:
    """Return a code name in the canonical ``lcm:<slot>:`` format."""
    base = name or f"Code Slot {slot_num}"
    return f"lcm:{slot_num}:{base}"


def make_compact_tagged_name(slot_num: int) -> str:
    """
    Return a code name in the compact ``lcm<slot>`` format.

    Charset-safe (alphanumeric only) fallback used when a lock's
    firmware rejects the colons in the canonical format. Drops the
    display portion but preserves the LCM ownership marker so the
    on-lock name doesn't collide with externally-created users in the
    way that the bare slot-only fallback would.
    """
    return f"lcm{slot_num}"


def parse_tag(name: str) -> tuple[int | None, str]:
    """
    Parse a Lock Code Manager slot tag, tolerant of all known formats.

    Returns ``(slot_num, friendly_name)`` when any format matches, or
    ``(None, original_name)`` otherwise. Match priority: canonical,
    legacy, compact, slot-only. The compact and slot-only branches are
    charset-/length-constrained fallbacks emitted by Matter (and
    eventually other providers) when the lock rejects the canonical
    name; their display portion is empty because the name only carries
    the slot binding. The legacy ``[LCM:<slot>]`` format is read-only
    -- nothing emits it anymore -- but is still recognized so older
    lock-stored names continue to identify as LCM-owned until the next
    write rewrites them in the canonical format. Bare digits being
    treated as a slot tag is intentional but ambiguous with external
    users whose names happen to be digit-only; the ambiguity is the
    cost of preserving the slot binding on constrained locks.
    """
    if match := _TAG_RE.match(name):
        return int(match.group(1)), match.group(2)
    if match := _LEGACY_SLOT_TAG_RE.match(name):
        return int(match.group(1)), match.group(2)
    if match := _COMPACT_TAG_RE.match(name):
        return int(match.group(1)), ""
    if match := _SLOT_ONLY_RE.match(name):
        return int(match.group(1)), ""
    return None, name


def foreign_slots(users: Iterable[User]) -> frozenset[int]:
    """
    Return the slots only users without a slot tag hold a PIN at.

    On a lock with user records, a PIN is Lock Code Manager's when its user
    carries a slot tag. One under an untagged user at credential index n is
    someone else's code at that index: the homeowner's own, made in the
    vendor's app, or a user Lock Code Manager made before it tagged them.
    Nothing on the lock tells those apart. A write of slot n adopts the
    user, tagging it and overwriting their PIN; a slot turned off leaves it
    alone, as a code nobody here manages (#1695).
    """
    tagged: set[int] = set()
    untagged: set[int] = set()
    for user in users:
        holders = untagged if parse_tag(user.name or "")[0] is None else tagged
        holders.update(credential.slot for credential in user.pin_credentials)
    return frozenset(untagged - tagged)


def parse_slot_num(value: object) -> int | None:
    """
    Convert a slot identifier to an int, or return None if not convertible.

    Mirrors ``int(value)`` while collapsing the ``TypeError``/``ValueError``
    that providers otherwise catch when a lock reports a non-numeric slot key
    (and ``OverflowError``, which ``int`` raises for infinite floats).
    JSON booleans are rejected rather than coerced (``int(True)`` is 1, so a
    malformed ``true`` would otherwise silently address slot 1).
    Call sites remain responsible for their own logging and skip/return flow.
    """
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except TypeError, ValueError, OverflowError:
        return None


def refuses_write_in_flight(write_slot: int | None, named_slot: int | None) -> bool:
    """
    Return whether a lock's duplicate refusal answers the PIN write in flight.

    User Code Command Class has no in-band duplicate result: a lock refusing
    a PIN another slot holds keeps the slot's old code and says so only with
    Access Control notification 15, which arrives while the write is still
    out. Some locks name no slot in it (no parameters, or user 0). Writes to
    a lock run one at a time, so one arriving during a write is that write's;
    one naming another slot, or arriving with no write in flight, is not
    ours to pin on a slot.
    """
    return write_slot is not None and named_slot in (None, 0, write_slot)


def is_masked_code(code: str) -> bool:
    """
    Return whether a code is a lock's placeholder for a code it will not show.

    Locks configured to withhold user codes answer reads with an asterisk per
    digit instead of the digits. That is a refusal, not a value: read as one,
    it never equals the configured Personal Identification Number, so sync
    sees a permanent mismatch and reprograms the slot until it is held.
    Withheld has to reach the worker as unreadable, which is
    exactly the state that means "occupied, contents unknown".

    Only an all-asterisk code counts; asterisks mixed with digits are not a
    shape any lock produces, and guessing at partial masking would discard a
    code that is really there.
    """
    return set(code) == {"*"}


def resolve_discovery_payload(
    hass: HomeAssistant, lock: er.RegistryEntry
) -> dict[str, Any] | None:
    """
    Return the MQTT discovery payload Home Assistant holds for a lock entity.

    An MQTT bridge publishes the exact topics it uses in its discovery
    payload, so reading them back is what lets a provider address custom,
    multi-level, and per-bridge base topics verbatim instead of rebuilding
    them from a device name. Which field to read is the caller's business --
    the bridges disagree about what a ``command_topic`` names -- but the walk
    down to the payload is the same for all of them.

    Returns None whenever no payload can be read: no device link, MQTT's
    debug data not loaded, the device carrying no entry for this entity, or
    a payload that is not a mapping. Callers must read that as disconnected
    rather than guess a topic.
    """
    device_id = lock.device_id
    if not device_id:
        return None
    try:
        info = mqtt_debug_info.info_for_device(hass, device_id)
    except KeyError:
        # MQTT integration data not loaded.
        LOGGER.debug("MQTT debug info unavailable for %s", lock.entity_id)
        return None
    entity_info = next(
        (
            candidate
            for candidate in info.get("entities", [])
            if candidate.get("entity_id") == lock.entity_id
        ),
        None,
    )
    if entity_info is None:
        return None
    payload = (entity_info.get("discovery_data") or {}).get("payload")
    if not isinstance(payload, dict):
        LOGGER.debug("No discovery payload for %s", lock.entity_id)
        return None
    return payload
