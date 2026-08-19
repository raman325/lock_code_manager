"""
Repointing automations and scripts at entity IDs that moved.

Home Assistant repoints recorder history when an entity ID changes, but an ID
written into an automation is just a string in a config file and nothing
rewrites it. The frontend's rename dialog offers to; a migration does not go
through the frontend.

What can be rewritten is bounded by where the configuration lives.
``automations.yaml`` and ``scripts.yaml`` are the files Home Assistant's own
config API owns and rewrites wholesale, so editing them is in keeping with how
they are already treated. Anything defined in ``configuration.yaml``, in a
package, in a dashboard or in a template belongs to somebody else and is
reported rather than touched: a fix that silently covers half the references
is worse than one that says what it missed, because the user stops looking.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.components import automation, script
from homeassistant.config import AUTOMATION_CONFIG_PATH, SCRIPT_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import load_yaml, save_yaml

# ``automations.yaml`` holds a list of automation dicts, each carrying its own
# ``id``; ``scripts.yaml`` holds a mapping of object id to script dict. Both
# are rewritten whole, which is what Home Assistant's config API does to them.
_LOGGER = logging.getLogger(__name__)

AUTOMATION_DOMAIN = "automation"
SCRIPT_DOMAIN = "script"

_FILES = {
    AUTOMATION_DOMAIN: AUTOMATION_CONFIG_PATH,
    SCRIPT_DOMAIN: SCRIPT_CONFIG_PATH,
}


@dataclass(slots=True)
class Referrers:
    """What still points at a moved entity ID, split by what can be fixed."""

    # Config keys, per domain, of entries in a file this can rewrite.
    fixable: dict[str, set[str]] = field(default_factory=dict)
    # How to describe those entries to the user.
    labels: list[str] = field(default_factory=list)
    # Entity IDs referencing a moved ID from somewhere this must not write.
    unfixable: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        """Return how many configs reference a moved ID."""
        return sum(len(found) for found in self.fixable.values()) + len(self.unfixable)


async def async_find_referrers(
    hass: HomeAssistant, moved: Mapping[str, str]
) -> Referrers:
    """
    Return what still points at a moved entity ID.

    The FILES are the source of truth for what can be fixed, not
    ``automations_with_entity``. That helper reads loaded automation entities
    and reports only ``referenced_entities``, so it misses an automation that
    failed to load and misses a blueprint input used solely inside a template
    -- which is exactly how the shipped Slot Usage blueprints use theirs.

    The loaded-entity lookup is still used, for the opposite job: finding
    references that are NOT in these files and therefore cannot be touched.
    """
    referrers = Referrers()
    for domain in _FILES:
        found = await hass.async_add_executor_job(_matching_keys, hass, domain, moved)
        if found:
            referrers.fixable[domain] = set(found)
            referrers.labels.extend(found.values())

    ent_reg = er.async_get(hass)
    for old_entity_id in moved:
        for domain, lookup in (
            (AUTOMATION_DOMAIN, automation.automations_with_entity),
            (SCRIPT_DOMAIN, script.scripts_with_entity),
        ):
            for referring in lookup(hass, old_entity_id):
                # A user-interface-managed automation or script is stored
                # under the key that becomes its unique ID; anything whose key
                # is not in the file is defined somewhere else.
                entry = ent_reg.async_get(referring)
                key = entry.unique_id if entry else None
                if key not in referrers.fixable.get(domain, set()):
                    referrers.unfixable.add(referring)
    return referrers


async def async_repoint(
    hass: HomeAssistant, moved: Mapping[str, str], referrers: Referrers
) -> int:
    """
    Rewrite the fixable files so they name the new entity IDs.

    Returns how many configs changed. Reloading is the caller's to do; this
    only touches files.
    """
    changed = 0
    for domain, keys in referrers.fixable.items():
        changed += await hass.async_add_executor_job(
            _repoint_file, hass, domain, keys, dict(moved)
        )
    return changed


def _entries(loaded: Any, domain: str) -> list[tuple[str, Any]]:
    """Return a config file's entries as ``(key, config)``, whatever its shape."""
    if domain == AUTOMATION_DOMAIN:
        return [
            (str(item["id"]), item)
            for item in loaded
            if isinstance(item, dict) and item.get("id") is not None
        ]
    return [(str(key), value) for key, value in loaded.items()]


def _matching_keys(
    hass: HomeAssistant, domain: str, moved: Mapping[str, str]
) -> dict[str, str]:
    """Return ``{key: label}`` for entries mentioning a moved ID."""
    return {
        key: str(config.get("alias") or key) if isinstance(config, dict) else key
        for key, config in _entries(_load(hass, domain), domain)
        if _mentions(config, moved)
    }


def _mentions(config: Any, moved: Mapping[str, str]) -> bool:
    """Return whether a moved entity ID appears anywhere inside ``config``."""
    if isinstance(config, str):
        return config in moved
    if isinstance(config, dict):
        return any(_mentions(value, moved) for value in config.values())
    if isinstance(config, list):
        return any(_mentions(item, moved) for item in config)
    return False


def _load(hass: HomeAssistant, domain: str) -> Any:
    """Read a config file, treating an absent or malformed one as empty."""
    empty: Any = [] if domain == AUTOMATION_DOMAIN else {}
    try:
        loaded = load_yaml(hass.config.path(_FILES[domain]))
    except FileNotFoundError, OSError, ValueError, HomeAssistantError:
        # A file that will not parse cannot be rewritten, and must not take
        # the whole repair down with it: an install part-way through an
        # upgrade is exactly where a broken automations file turns up, and
        # the other file may still be fixable.
        _LOGGER.warning(
            "Could not read %s, so nothing in it can be repointed",
            _FILES[domain],
            exc_info=True,
        )
        return empty
    return loaded if isinstance(loaded, type(empty)) else empty


def _repoint_file(
    hass: HomeAssistant, domain: str, keys: set[str], moved: dict[str, str]
) -> int:
    """Rewrite one config file's matching entries. Runs in an executor."""
    loaded = _load(hass, domain)
    changed = sum(
        _substitute(config, moved)
        for key, config in _entries(loaded, domain)
        if key in keys
    )
    if changed:
        save_yaml(hass.config.path(_FILES[domain]), loaded)
    return changed


def _substitute(config: Any, moved: Mapping[str, str]) -> bool:
    """
    Replace every moved entity ID inside ``config``, in place.

    Matches WHOLE strings only. An entity ID can appear as a bare value or in
    a list, and a substring replacement would corrupt a template that merely
    mentions one.
    """
    if isinstance(config, dict):
        items: Iterable[tuple[Any, Any]] = list(config.items())
    elif isinstance(config, list):
        items = list(enumerate(config))
    else:
        return False

    replaced = False
    for key, value in items:
        if isinstance(value, str) and value in moved:
            config[key] = moved[value]
            replaced = True
        elif _substitute(value, moved):
            replaced = True
    return replaced


def format_moved(moved: Mapping[str, str]) -> str:
    """Return the moved IDs as a markdown list for a repair description."""
    return "\n".join(f"- `{was}` is now `{now}`" for was, now in sorted(moved.items()))


def format_entities(hass: HomeAssistant, entity_ids: Iterable[str]) -> str:
    """Return entities as a markdown list, named as the user sees them."""
    return "\n".join(
        f"- {state.name if (state := hass.states.get(entity_id)) else entity_id}"
        f" (`{entity_id}`)"
        for entity_id in sorted(entity_ids)
    )
