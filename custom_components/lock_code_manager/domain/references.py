"""
Finding what still points at an entity ID that moved.

Home Assistant repoints recorder history when an entity ID changes, but an ID
written into an automation is just a string in a config file and nothing
rewrites it. The frontend's rename dialog offers to; a migration does not go
through the frontend.

This only ever READS. Rewriting somebody's ``automations.yaml`` was tried and
abandoned: saving the file back resolves away whatever the loader resolved on
the way in, so an ``!include`` returns inlined with the file it pointed at
orphaned, and a ``!secret`` returns as the secret in plain text. Four rounds of
review found a new way for it to damage a configuration each time, to save the
user re-picking an entity in a dialog. Telling them precisely what to look at
is the part that was worth having.

Two sources, because neither sees everything:

* Home Assistant knows which loaded automations and scripts reference an
  entity, including ones defined anywhere at all -- but only as
  ``referenced_entities``, computed from the rendered config, which does not
  include IDs used inside a template.
* Reading the managed files catches those, and catches configs that failed to
  load, but only for the two files Home Assistant's own config API owns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any

from homeassistant.components import automation, persistent_notification, script
from homeassistant.config import AUTOMATION_CONFIG_PATH, SCRIPT_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.yaml import load_yaml

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

AUTOMATION_DOMAIN = "automation"
SCRIPT_DOMAIN = "script"

# ``automations.yaml`` holds a list of automation dicts, each carrying its own
# ``id``; ``scripts.yaml`` holds a mapping of object id to script dict.
_FILES = {
    AUTOMATION_DOMAIN: AUTOMATION_CONFIG_PATH,
    SCRIPT_DOMAIN: SCRIPT_CONFIG_PATH,
}


@dataclass(frozen=True, slots=True)
class Referrers:
    """What still points at a moved entity ID, as the user would name it."""

    labels: tuple[str, ...]

    @property
    def total(self) -> int:
        """Return how many automations and scripts still point at one."""
        return len(self.labels)


async def async_find_referrers(
    hass: HomeAssistant, moved: Mapping[str, str]
) -> Referrers:
    """
    Return everything still pointing at a moved entity ID.

    Runs when the user opens the repair rather than during the migration:
    ``automations_with_entity`` reads the LOADED automation entities, and
    while a config entry is setting up the automation component may not have
    started.
    """
    labels: set[str] = set()
    for domain in _FILES:
        labels |= await hass.async_add_executor_job(
            _labels_in_file, hass, domain, moved
        )

    for old_entity_id in moved:
        for lookup in (
            automation.automations_with_entity,
            script.scripts_with_entity,
        ):
            for entity_id in lookup(hass, old_entity_id):
                state = hass.states.get(entity_id)
                labels.add(state.name if state else entity_id)

    return Referrers(labels=tuple(sorted(labels)))


def _labels_in_file(
    hass: HomeAssistant, domain: str, moved: Mapping[str, str]
) -> set[str]:
    """Return how to name each config in one file that mentions a moved ID."""
    return {
        str(config.get("alias") or key) if isinstance(config, dict) else key
        for key, config in _entries(_load(hass, domain), domain)
        if _mentions(config, moved)
    }


def _entries(loaded: Any, domain: str) -> list[tuple[str, Any]]:
    """Return a config file's entries as ``(key, config)``, whatever its shape."""
    if domain == AUTOMATION_DOMAIN:
        return [
            (str(item["id"]), item)
            for item in loaded
            if isinstance(item, dict) and item.get("id") is not None
        ]
    return [(str(key), value) for key, value in loaded.items()]


def _mentions(config: Any, moved: Mapping[str, str]) -> bool:
    """
    Return whether a moved entity ID appears anywhere inside ``config``.

    Keys as well as values, and inside longer strings as well as whole ones:
    an ID can be the key in ``entities: {text.raman_pin: ...}`` and can sit
    inside ``{{ states('text.raman_pin') }}``. The boundaries treat ``_`` and
    ``.`` as parts of an ID, so a longer ID that merely begins with a moved
    one is somebody else.
    """
    if isinstance(config, str):
        return any(
            re.search(rf"(?<![\w.]){re.escape(old)}(?![\w.])", config) for old in moved
        )
    if isinstance(config, dict):
        return any(
            _mentions(key, moved) or _mentions(value, moved)
            for key, value in config.items()
        )
    if isinstance(config, list):
        return any(_mentions(item, moved) for item in config)
    return False


def _load(hass: HomeAssistant, domain: str) -> Any:
    """Read a config file, treating an absent or malformed one as empty."""
    empty: Any = [] if domain == AUTOMATION_DOMAIN else {}
    path = hass.config.path(_FILES[domain])
    if not Path(path).exists():
        return empty
    try:
        loaded = load_yaml(path)
    except OSError, ValueError, HomeAssistantError:
        # A file that will not parse is one this cannot read for references.
        # Home Assistant still reports whatever it managed to load from it.
        _LOGGER.warning(
            "Could not read %s while looking for references to renamed "
            "entities; anything defined in it may need checking by hand",
            _FILES[domain],
            exc_info=True,
        )
        return empty
    return loaded if isinstance(loaded, type(empty)) else empty


def format_moved(moved: Mapping[str, str]) -> str:
    """Return the moved IDs as a markdown list for a repair description."""
    return "\n".join(f"- `{was}` is now `{now}`" for was, now in sorted(moved.items()))


def format_labels(labels: Iterable[str]) -> str:
    """Return the referring configs as a markdown list."""
    return "\n".join(f"- {label}" for label in labels) or "- (none found)"


async def async_notify_moved(hass: HomeAssistant, moved: Mapping[str, str]) -> None:
    """
    Tell the user which entity IDs moved, and what still points at the old ones.

    A notification rather than a repair, because there is nothing here to
    repair: rewriting somebody's ``automations.yaml`` was tried and abandoned,
    so this only ever reported. A repair offers a fix and waits to be
    resolved; this is a thing to read once and dismiss.

    Deferred until Home Assistant has started, which is the whole reason it
    cannot be raised from the migration: the lookup below needs the
    automation and script components to have loaded their configuration, and
    during a config entry's setup they may not have.
    """
    referrers = await async_find_referrers(hass, moved)
    lines = [
        "Lock Code Manager identifies each user by name rather than by slot "
        "number, so these entity IDs were renamed:",
        "",
        format_moved(dict(moved)),
        "",
        "History and statistics moved with them, and dashboards built by Lock "
        "Code Manager keep working.",
    ]
    if referrers.total:
        lines += [
            "",
            "**These still refer to the old IDs and need updating**",
            "",
            format_labels(referrers.labels),
            "",
            "For an automation built from a Lock Code Manager blueprint, the "
            "quickest fix is to open it and pick the entity again, or recreate "
            "it. Lock Code Manager does not edit your automations for you.",
        ]
    else:
        # Worth saying rather than leaving the reader to wonder whether it
        # was checked: nothing of theirs has to change.
        lines += ["", "Nothing else in your configuration refers to the old IDs."]

    persistent_notification.async_create(
        hass,
        "\n".join(lines),
        title="Lock Code Manager entity IDs have been renamed",
        notification_id=f"{DOMAIN}_entity_ids_renamed",
    )
