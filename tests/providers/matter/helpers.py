"""Matter test helpers — mirrors homeassistant/tests/components/matter/common.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from matter_server.client.models.node import MatterNode
from matter_server.common.helpers.util import dataclass_from_dict
from matter_server.common.models import MatterNodeData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "nodes"


def create_node_from_fixture(fixture_name: str) -> MatterNode:
    """Create a MatterNode from a JSON fixture file."""
    fixture_path = FIXTURES_DIR / f"{fixture_name}.json"
    node_data = json.loads(fixture_path.read_text())
    return MatterNode(dataclass_from_dict(MatterNodeData, node_data))


async def setup_matter_integration_with_node(
    hass: HomeAssistant,
    client: MagicMock,
    node: MatterNode,
) -> MockConfigEntry:
    """Set up the Matter integration with a single node."""
    client.get_nodes.return_value = [node]
    client.get_node.side_effect = lambda node_id: (
        node if node.node_id == node_id else None
    )

    entry = MockConfigEntry(domain="matter", data={"url": "ws://localhost:5580/ws"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
