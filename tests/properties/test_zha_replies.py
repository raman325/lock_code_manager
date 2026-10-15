"""
Property-based tests for how the ZHA provider attributes a zigpy reply.

zigpy fills a pending request with whatever frame arrives on the same
endpoint, cluster, direction and transaction sequence number, so a reply
handed to the provider is not necessarily the answer to the command it sent.
These pin what may count as an answer, over replies built the way zigpy
decodes them -- from the stock Door Lock cluster or a quirk's -- rather than
over the handful of shapes the example tests name.
"""

from collections.abc import Callable
from typing import Any, NamedTuple

from hypothesis import example, given, strategies as st
import pytest
from zigpy.zcl import foundation
from zigpy.zcl.clusters.closures import DoorLock

from custom_components.lock_code_manager.domain.exceptions import LockOperationFailed
from custom_components.lock_code_manager.providers.zha import ZHALock, _write_answer
from tests.providers.zha.helpers import (
    QuirkedDoorLock,
    RenamingQuirkDoorLock,
    default_response,
    lock_frame,
)

# Small enough that a reply and the slot asked collide often.
_users = st.integers(min_value=0, max_value=4)
_CLUSTER_TYPES = (DoorLock, QuirkedDoorLock, RenamingQuirkDoorLock)
_clusters = st.sampled_from(_CLUSTER_TYPES)
_codes = st.text(alphabet="0123456789*", max_size=8)
_statuses = st.sampled_from(list(foundation.Status))


class _Drawn(NamedTuple):
    """A reply, the cluster that decoded it, and what it was built as."""

    kind: str
    cluster: type[DoorLock]
    reply: Any


@st.composite
def _replies(draw: st.DrawFn) -> _Drawn:
    """Draw a frame the lock could send back on a pending request's sequence number."""
    cluster = draw(_clusters)
    kind = draw(
        st.sampled_from(
            [
                "get_pin_code_response",
                "set_pin_code_response",
                "clear_pin_code_response",
                "operation_event_notification",
                "set_week_day_schedule_response",
                "default_response",
                "undecoded_bytes",
            ]
        )
    )
    if kind == "get_pin_code_response":
        reply = lock_frame(
            cluster,
            kind,
            user_id=draw(_users),
            user_status=draw(st.sampled_from(list(DoorLock.UserStatus))),
            user_type=draw(st.sampled_from(list(DoorLock.UserType))),
            code=draw(_codes),
        ).payload
    elif kind in (
        "set_pin_code_response",
        "clear_pin_code_response",
        # Shares the Default Response's command id, and carries a status.
        "set_week_day_schedule_response",
    ):
        reply = lock_frame(cluster, kind, status=draw(_statuses)).payload
    elif kind == "operation_event_notification":
        reply = lock_frame(
            cluster,
            kind,
            operation_event_source=draw(
                st.sampled_from(list(DoorLock.OperationEventSource))
            ),
            operation_event_code=draw(st.sampled_from(list(DoorLock.OperationEvent))),
            user_id=draw(_users),
            pin=draw(_codes),
            local_time=0,
        ).payload
    elif kind == "default_response":
        reply = default_response(
            draw(st.integers(min_value=0, max_value=0x0F)), draw(_statuses)
        ).payload
    else:
        # A command the cluster has no definition for reaches the request as bytes.
        reply = draw(st.binary(max_size=8))
    return _Drawn(kind, cluster, reply)


def _examples(*cases: dict[str, Any]) -> Callable[[Any], Any]:
    """Pin each case as an explicit example of the property it decorates."""

    def apply(test: Any) -> Any:
        for case in cases:
            test = example(**case)(test)
        return test

    return apply


def _drawn(cluster: type[DoorLock], kind: str, **fields: Any) -> _Drawn:
    """Build ``kind`` as ``cluster`` decodes it."""
    return _Drawn(kind, cluster, lock_frame(cluster, kind, **fields).payload)


_SUCCESS = foundation.Status.SUCCESS


# Replies no end-to-end test sends a read, pinned rather than left to chance:
# a write's response and a Default Response, each saying SUCCESS, and raw
# bytes.
@_examples(
    *(
        {
            "drawn": _drawn(cluster, "set_pin_code_response", status=_SUCCESS),
            "slot_num": 3,
        }
        for cluster in _CLUSTER_TYPES
    ),
    {
        "drawn": _Drawn(
            "default_response",
            DoorLock,
            default_response(
                DoorLock.ServerCommandDefs.get_pin_code.id, _SUCCESS
            ).payload,
        ),
        "slot_num": 3,
    },
    {"drawn": _Drawn("undecoded_bytes", DoorLock, b"unexpected"), "slot_num": 3},
)
@given(drawn=_replies(), slot_num=_users)
def test_only_the_slots_own_reply_answers_a_read(drawn: _Drawn, slot_num: int) -> None:
    """
    A read is answered only by a Get PIN Code Response naming the slot asked.

    Any other frame -- another user's reply, an event, a write's response, a
    Default Response -- is no answer, from a quirk's cluster as from the
    stock one, and the answer it gives is the reply's own status and code.
    """
    parsed = ZHALock._parse_pin_response(drawn.reply, slot_num, drawn.cluster)
    answers = drawn.kind == "get_pin_code_response" and (
        drawn.reply.user_id == slot_num
    )
    assert (parsed is not None) == answers
    if parsed is not None:
        assert parsed == (drawn.reply.user_status, str(drawn.reply.code))


_writes = st.sampled_from(
    [
        (
            DoorLock.ServerCommandDefs.set_pin_code,
            DoorLock.ClientCommandDefs.set_pin_code_response,
        ),
        (
            DoorLock.ServerCommandDefs.clear_pin_code,
            DoorLock.ClientCommandDefs.clear_pin_code_response,
        ),
    ]
)


_SET = (
    DoorLock.ServerCommandDefs.set_pin_code,
    DoorLock.ClientCommandDefs.set_pin_code_response,
)
_CLEAR = (
    DoorLock.ServerCommandDefs.clear_pin_code,
    DoorLock.ClientCommandDefs.clear_pin_code_response,
)


# A frame about something else that says SUCCESS -- the other write's
# response, or a cluster command sharing the Default Response's id -- is a
# false confirmation, pinned per cluster. Raw bytes are never decoded, so one
# cluster stands for all.
@_examples(
    *(
        {"drawn": _drawn(cluster, kind, status=_SUCCESS), "write": write}
        for cluster in _CLUSTER_TYPES
        for write, kind in (
            (_SET, "clear_pin_code_response"),
            (_CLEAR, "set_pin_code_response"),
            (_SET, "set_week_day_schedule_response"),
            (_CLEAR, "set_week_day_schedule_response"),
        )
    ),
    *(
        {"drawn": _Drawn("undecoded_bytes", DoorLock, b"\x01"), "write": write}
        for write in (_SET, _CLEAR)
    ),
)
@given(drawn=_replies(), write=_writes)
def test_only_the_writes_own_reply_answers_it(
    drawn: _Drawn,
    write: tuple[foundation.ZCLCommandDef, foundation.ZCLCommandDef],
) -> None:
    """
    A set or clear is answered only by the reply that is about it.

    That is its own response command, as the lock's cluster decodes it, or a
    Default Response naming the command sent, and the answer is that reply
    itself. Any other frame, however it carries a status, leaves the write's
    outcome unknown, so the write fails to be sent again.
    """
    request, response = write
    answers = drawn.kind == response.name or (
        drawn.kind == "default_response" and drawn.reply.command_id == request.id
    )
    if not answers:
        with pytest.raises(LockOperationFailed):
            _write_answer(drawn.reply, drawn.cluster, request, response, 1, "lock.test")
        return
    assert (
        _write_answer(drawn.reply, drawn.cluster, request, response, 1, "lock.test")
        is drawn.reply
    )
