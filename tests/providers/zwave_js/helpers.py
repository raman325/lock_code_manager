"""Shared Z-Wave JS provider test helpers: patch paths, event factories, entity lookups."""

from typing import Any

from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.access_control import UserCredentialType
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


def get_enabled_switch_entity_id(hass: HomeAssistant, entry_id: str, slot: int) -> str:
    """Return the enabled-switch entity ID for a slot, asserting it exists."""
    ent_reg = er.async_get(hass)
    uid = f"{entry_id}|{slot}|{CONF_ENABLED}"
    entity_id = ent_reg.async_get_entity_id("switch", DOMAIN, uid)
    assert entity_id, f"Switch entity not found for slot {slot}"
    return entity_id


def make_uc_value_event(
    node_id: int, property_name: str, code_slot: int, new_value: Any
) -> ZwaveEvent:
    """Create a User Code Command Class value-updated event."""
    return ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": property_name,
                "propertyKey": code_slot,
                "newValue": new_value,
            },
        },
    )


def _make_access_control_event(
    node_id: int, event_name: str, args: dict[str, Any]
) -> ZwaveEvent:
    """Create one of the driver's unified access-control node events."""
    return ZwaveEvent(
        type=event_name,
        data={
            "source": "node",
            "event": event_name,
            "nodeId": node_id,
            "endpointIndex": 0,
            "args": args,
        },
    )


def make_credential_added_event(
    node_id: int,
    credential_slot: int,
    credential_type: int = UserCredentialType.PIN_CODE,
    user_id: int = 1,
    credential_data: str | None = None,
) -> ZwaveEvent:
    """Build a 'credential added' ZwaveEvent for a given slot."""
    args: dict = {
        "userId": user_id,
        "credentialType": credential_type,
        "credentialSlot": credential_slot,
    }
    if credential_data is not None:
        args["data"] = credential_data
    return _make_access_control_event(node_id, "credential added", args)


def make_credential_modified_event(
    node_id: int,
    credential_slot: int,
    credential_type: int = UserCredentialType.PIN_CODE,
    user_id: int = 1,
    credential_data: str | None = None,
) -> ZwaveEvent:
    """Build a 'credential modified' ZwaveEvent for a given slot."""
    args: dict = {
        "userId": user_id,
        "credentialType": credential_type,
        "credentialSlot": credential_slot,
    }
    if credential_data is not None:
        args["data"] = credential_data
    return _make_access_control_event(node_id, "credential modified", args)


def make_credential_deleted_event(
    node_id: int,
    credential_slot: int,
    credential_type: int = UserCredentialType.PIN_CODE,
    user_id: int = 1,
) -> ZwaveEvent:
    """Build a 'credential deleted' ZwaveEvent for a given slot."""
    return _make_access_control_event(
        node_id,
        "credential deleted",
        {
            "userId": user_id,
            "credentialType": credential_type,
            "credentialSlot": credential_slot,
        },
    )


def make_user_deleted_event(node_id: int, user_id: int) -> ZwaveEvent:
    """Build a 'user deleted' ZwaveEvent for a given user."""
    return _make_access_control_event(node_id, "user deleted", {"userId": user_id})
