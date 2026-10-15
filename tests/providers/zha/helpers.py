"""Zigbee frames and quirk-style clusters shared by the ZHA provider tests."""

from typing import Any, NamedTuple

from zhaquirks.clusters import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.clusters import closures
from zigpy.zcl.foundation import Status

_DOOR_LOCK_REPLIES = (
    closures.DoorLock.ClientCommandDefs.get_pin_code_response,
    closures.DoorLock.ClientCommandDefs.set_pin_code_response,
    closures.DoorLock.ClientCommandDefs.clear_pin_code_response,
)


def _redefined(
    stock: foundation.ZCLCommandDef,
    *,
    name: str | None = None,
    schema: dict[str, Any] | None = None,
) -> foundation.ZCLCommandDef:
    """Redefine a stock Door Lock reply as a quirk does, under the same id."""
    return foundation.ZCLCommandDef(
        id=stock.id,
        name=stock.name if name is None else name,
        schema=(
            {field.name: field.type for field in stock.schema.fields}
            if schema is None
            else schema
        ),
        direction=stock.direction,
    )


class QuirkedDoorLock(CustomCluster, closures.DoorLock):
    """
    A Door Lock cluster whose replies a quirk redefines, as zha-quirks does.

    zigpy compiles each redefined command into a schema class of its own, so
    a reply this cluster decodes is the same command as the stock one without
    being an instance of the stock class.
    """

    client_commands = {
        **closures.DoorLock.client_commands,
        **{stock.id: _redefined(stock) for stock in _DOOR_LOCK_REPLIES},
    }


class RenamingQuirkDoorLock(CustomCluster, closures.DoorLock):
    """A Door Lock cluster whose quirk redefines the replies under other names."""

    client_commands = {
        **closures.DoorLock.client_commands,
        **{
            stock.id: _redefined(stock, name=f"vendor_{stock.name}")
            for stock in _DOOR_LOCK_REPLIES
        },
    }


def quirk_redefining(reply: str, schema: dict[str, Any]) -> type[closures.DoorLock]:
    """
    Return a quirk's Door Lock cluster that decodes the stock ``reply`` with ``schema``.

    For the reply shapes a quirk can produce: a field named otherwise, an
    optional field, or a field decoded as another type.
    """
    stock = getattr(closures.DoorLock.ClientCommandDefs, reply)
    return type(
        f"{reply}_quirk",
        (CustomCluster, closures.DoorLock),
        {
            "client_commands": {
                **closures.DoorLock.client_commands,
                stock.id: _redefined(stock, schema=schema),
            }
        },
    )


class Frame(NamedTuple):
    """A Zigbee Cluster Library frame the lock sends: its payload, and whether general."""

    general: bool
    payload: foundation.CommandSchema


def lock_frame(
    cluster: closures.DoorLock | type[closures.DoorLock], name: str, **fields: Any
) -> Frame:
    """
    Build a Door Lock command the lock sends, from the cluster's own definition.

    ``name`` is the stock command's name; the lock sends its id, which the
    cluster's own table may define under another name.
    """
    command = cluster.client_commands[
        getattr(closures.DoorLock.ClientCommandDefs, name).id
    ]
    return Frame(general=False, payload=command.schema(**fields))


def default_response(command_id: int, status: Status) -> Frame:
    """Build a Default Response naming ``command_id``."""
    command = foundation.GENERAL_COMMANDS[foundation.GeneralCommand.Default_Response]
    return Frame(
        general=True, payload=command.schema(command_id=command_id, status=status)
    )
