"""Z-Wave JS provider test helpers — event factories and entity-id lookups."""

from __future__ import annotations

from typing import Any

from zwave_js_server.event import Event as ZwaveEvent

from homeassistant.const import CONF_ENABLED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN


def async_capture_events(
    hass: HomeAssistant, event_name: str
) -> list[Event[dict[str, Any]]]:
    """Capture events of the given type on the hass event bus."""
    events: list[Event[dict[str, Any]]] = []

    @callback
    def capture_events(event: Event[dict[str, Any]]) -> None:
        events.append(event)

    hass.bus.async_listen(event_name, capture_events)
    return events


def make_duplicate_code_event(node_id: int, user_id: int | None = None) -> ZwaveEvent:
    """Create a duplicate-code Access Control notification ZwaveEvent."""
    params: dict[str, Any] = {}
    if user_id is not None:
        params["userId"] = user_id
    return ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": 6,  # ACCESS_CONTROL
                "event": 15,  # NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE
                "label": "Access Control",
                "eventLabel": "New user code not added due to duplicate code",
                "parameters": params,
            },
        },
    )


def get_enabled_switch_entity_id(hass: HomeAssistant, entry_id: str, slot: int) -> str:
    """Return the enabled-switch entity ID for a slot, asserting it exists."""
    ent_reg = er.async_get(hass)
    uid = f"{entry_id}|{slot}|{CONF_ENABLED}"
    entity_id = ent_reg.async_get_entity_id("switch", DOMAIN, uid)
    assert entity_id, f"Switch entity not found for slot {slot}"
    return entity_id
