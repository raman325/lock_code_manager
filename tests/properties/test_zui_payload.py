"""
Property-based tests for zwave-js-ui payload unwrapping and projection.

The gateway's payload-type setting decides, per installation, which of three
shapes a value topic publishes in; ``_unwrap_mqtt_value`` must recover the
bare value from any of them and must never raise on whatever bytes MQTT
happens to hand it. ``_project_user_code_result`` sits downstream of a
``get`` API call whose result shape is not otherwise validated before it
reaches the projection, so it must also be total. Example-based tests in
``tests/providers/zwave_js_ui/test_payload.py`` pin the documented cases;
these pin the invariants over the whole input space.
"""

from __future__ import annotations

import json

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.domain.credentials import WriteResult
from custom_components.lock_code_manager.domain.exceptions import LockOperationFailed
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js_ui import (
    _project_user_code_result,
    _supervised_write_result,
    _unwrap_mqtt_value,
)

# Recursive JSON-encodable value strategy: the leaves are the scalar types
# JSON can carry, and dicts/lists nest them. `allow_nan=False` because
# `json.dumps` on NaN produces non-standard JSON that a real gateway (and
# `json.loads`) would not round-trip identically.
_json_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
)
json_value = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(st.text(), children, max_size=5)
    ),
    max_leaves=10,
)


@given(value=json_value)
def test_unwrap_round_trips_the_time_value_wrapper(value: object) -> None:
    """The ``{time, value}`` wrapper shape always yields back its ``value``."""
    payload = json.dumps({"time": 0, "value": value}).encode()
    assert _unwrap_mqtt_value(payload) == value


@given(raw=st.binary())
def test_unwrap_never_raises_on_arbitrary_bytes(raw: bytes) -> None:
    """Totality: MQTT can hand this function anything a broker will carry."""
    _unwrap_mqtt_value(raw)


@given(result=json_value)
def test_project_user_code_result_is_total(result: object) -> None:
    """Totality: the API client passes through whatever the gateway returns."""
    assert isinstance(_project_user_code_result(result), SlotCredential)


# What a supervised ``set``/``clear`` hands back: any JSON at all, plus the
# Supervision result shapes node-zwave-js produces, so the statuses that
# matter are generated rather than left to chance.
_write_results = json_value | st.fixed_dictionaries(
    {"status": st.sampled_from([0, 1, 2, 255]) | json_value}
)


@given(result=_write_results)
def test_only_supervision_success_confirms_a_write(result: object) -> None:
    """
    A write is the lock's word only when the lock said Supervision Success.

    Anything else the gateway can return is at most unconfirmed, and only an
    explicit Fail is a refusal.
    """
    status = result.get("status") if isinstance(result, dict) else None
    named = None if isinstance(status, bool) else status
    try:
        outcome = _supervised_write_result("set", 1, result)
    except LockOperationFailed:
        assert named == 2
        return
    assert (outcome is WriteResult.CONFIRMED) == (named == 255)
    assert outcome is not WriteResult.NO_CHANGE
