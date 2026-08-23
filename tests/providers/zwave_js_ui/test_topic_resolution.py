"""Tests for zwave-js-ui device identifier and topic resolution."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.providers.zwave_js_ui import (
    parse_zwave_js_ui_identifier,
)


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("zwavejs2mqtt_0xd4ee5a7a_node20", ("0xd4ee5a7a", 20)),
        ("zwavejs2mqtt_0xABCD1234_node1", ("0xabcd1234", 1)),
        ("zigbee2mqtt_0xc0ffee", None),
        ("zwavejs2mqtt_0xd4ee5a7a", None),
        ("zwavejs2mqtt_d4ee5a7a_node20", None),
        ("zwavejs2mqtt_0xd4ee5a7a_node20_extra", None),
    ],
)
def test_parse_zwave_js_ui_identifier(identifier, expected):
    """Home hex is normalized to lowercase; malformed identifiers parse to None."""
    assert parse_zwave_js_ui_identifier(identifier) == expected
