"""Tests for zwave-js-ui payload unwrapping and credential projection."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.domain.exceptions import LockOperationFailed
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js_ui import (
    _project_user_code_result,
    _raise_on_supervision_fail,
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
        # A lock configured to withhold codes answers with one asterisk per
        # digit. Projected known, it never equals the configured PIN, so sync
        # reprograms the slot on every tick forever.
        ({"userIdStatus": 1, "userCode": "****"}, SlotCredential.unreadable()),
        ({"userIdStatus": 1, "userCode": "*"}, SlotCredential.unreadable()),
        # Only an all-asterisk code is a mask; asterisks mixed with digits are
        # not a shape any lock produces, and guessing at partial masking would
        # discard a code that is really there.
        ({"userIdStatus": 1, "userCode": "12**"}, SlotCredential.known("12**")),
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


@pytest.mark.parametrize("operation", ["set", "clear"])
def test_a_supervision_fail_refuses_the_write(operation):
    """Supervision status Fail (2) is the lock refusing the command."""
    with pytest.raises(LockOperationFailed, match="refused by the lock"):
        _raise_on_supervision_fail(operation, 3, {"status": 2})


@pytest.mark.parametrize("operation", ["set", "clear"])
@pytest.mark.parametrize(
    "result",
    [
        # node-zwave-js SupervisionStatus: 0 NoSupport, 1 Working, 255 Success.
        pytest.param({"status": 255}, id="success"),
        pytest.param(
            {"status": 1, "remainingDuration": {"unit": "seconds", "value": 5}},
            id="working",
        ),
        pytest.param({"status": 0}, id="no_support"),
        # No result at all: the command went out unsupervised.
        pytest.param(None, id="unsupervised"),
        pytest.param({}, id="no_status"),
        # ``True == 1`` and ``False == 0``: neither can equal Fail, and ``2``
        # is only a refusal as a number.
        pytest.param({"status": True}, id="boolean_true_status"),
        pytest.param({"status": False}, id="boolean_false_status"),
        pytest.param({"status": "2"}, id="string_status"),
        pytest.param([2], id="list_result"),
        pytest.param(2, id="bare_number"),
    ],
)
def test_any_other_result_leaves_the_write_standing(operation, result):
    """Only an explicit Fail changes what a successful api call means."""
    _raise_on_supervision_fail(operation, 3, result)
