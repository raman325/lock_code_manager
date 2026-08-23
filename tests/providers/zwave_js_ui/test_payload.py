"""Tests for zwave-js-ui payload unwrapping and credential projection."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js_ui import (
    _project_user_code_result,
    _unwrap_mqtt_value,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"1234", 1234),  # bare number json-parses
        (b'"1234"', "1234"),  # raw JSON string
        (b"bare text", "bare text"),  # non-JSON is the raw value
        (b'{"time": 1, "value": "1234"}', "1234"),  # time-value wrapper
        (
            b'{"value": "1234", "id": "20-99-0-userCode-1"}',
            "1234",
        ),  # full valueId object
        (b'{"time": 1, "value": {"userId": 3}}', {"userId": 3}),
        (b'{"time": 1, "value": null}', None),
        (b'{"nested": {"value": 1}}', {"nested": {"value": 1}}),  # no top-level value
        # An empty payload is how MQTT clears a retained message, not a value.
        (b"", None),
        ("1234", 1234),  # str input works too
    ],
)
def test_unwrap_mqtt_value(raw, expected):
    """All three gateway payload shapes unwrap to the bare value."""
    assert _unwrap_mqtt_value(raw) == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"userIdStatus": 1, "userCode": "1234"}, SlotCredential.known("1234")),
        ({"userIdStatus": 0}, SlotCredential.empty()),
        ({"userIdStatus": 0, "userCode": ""}, SlotCredential.empty()),
        ({"userIdStatus": 1}, SlotCredential.unreadable()),
        ({"userIdStatus": 1, "userCode": "   "}, SlotCredential.unreadable()),
        (
            {"userIdStatus": 1, "userCode": {"type": "Buffer", "data": [1, 2]}},
            SlotCredential.unreadable(),
        ),
        ({"userIdStatus": 2, "userCode": "9999"}, SlotCredential.unreadable()),
        ({"userIdStatus": 254}, SlotCredential.unreadable()),
        # JSON ``true`` == 1 in Python; it must not masquerade as Enabled.
        ({"userIdStatus": True, "userCode": "1234"}, SlotCredential.unreadable()),
        ("nonsense", SlotCredential.unreadable()),
        (None, SlotCredential.unreadable()),
        ([], SlotCredential.unreadable()),
    ],
)
def test_project_user_code_result(result, expected):
    """Only Available is empty; Enabled needs a usable string code to be known."""
    assert _project_user_code_result(result) == expected
