"""The unified credential-used bus event."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant, callback

from ..const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIG_ENTRY_TITLE,
    ATTR_SOURCE,
    ATTR_TARGET,
    BUS_EVENT_CREDENTIAL_USED,
)


@callback
def async_fire_credential_used(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    *,
    name: str,
    source: str,
    target: str,
) -> None:
    """
    Announce that a credential belonging to ``name`` was used.

    One event for both ways a use becomes known: a lock that observed it, and
    a caller that recorded one entered somewhere this integration cannot see.
    The user is the subject, so the payload names the person rather than the
    slot number they happen to occupy -- that number is internal bookkeeping
    and putting it here would tie every consumer to it.

    All five fields are always present and never empty. ``source`` (where the
    credential was entered) and ``target`` (what it was used against) are the
    same entity when a lock observed the use itself. Neither is restricted to
    a domain: a use can target a lock, a cover, an alarm panel, anything the
    caller associates it with. A caller with no natural entity for one of
    them is expected to supply one -- a template entity as a source, a
    Virtual lock as a target. That is the deliberate trade: a rigid payload
    every consumer can read without key tests or null checks, with documented
    escape hatches for the setups that need them. Do not add a default, a
    fallback, or an optional field here.

    Nothing dereferences ``source`` or ``target``: a code source's own state
    can be the cleartext credential that was typed, so both travel as bare
    identifiers and are never looked up or read.
    """
    hass.bus.async_fire(
        BUS_EVENT_CREDENTIAL_USED,
        event_data={
            ATTR_NAME: name,
            ATTR_CONFIG_ENTRY_ID: config_entry.entry_id,
            ATTR_CONFIG_ENTRY_TITLE: config_entry.title,
            ATTR_SOURCE: source,
            ATTR_TARGET: target,
        },
    )
