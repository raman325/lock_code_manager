"""
Property-based tests for the identifier remap algorithm.

The codec had properties from the start; the algorithm that *drives* it did
not, and the gap cost a hang. `_async_remap_segment` resolves a mapping by
repeating until a pass moves nothing, parking a cycle member when it stalls.
Two successive versions of the stall handler looped forever:

* the first re-parked an already-resolved segment whose target happened to
  remain a mapping key;
* the second treated "target is also due to move" as a cycle -- which is
  true of every *chain* -- so it parked a chain member, re-detected the same
  condition on the parked segment, and parked again without bound.

Both are termination bugs, and termination is a property. Neither survives
one second of Hypothesis; neither was caught by eight rounds of review plus
hand-written cycle tests, because a hand-written test only explores the
shapes its author already thought of.

These exercise the mapping algebra directly rather than through the
registries. The hazard lives in the algebra: whether a park makes progress
is a question about the mapping, not about Home Assistant.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from custom_components.lock_code_manager.domain.identifier_migration import (
    _cycle_member,
    _temporary_segment,
)

# Small alphabet so cycles and chains actually occur rather than every name
# being unique and the mapping trivially resolvable.
SEGMENTS = st.sampled_from(["a", "b", "c", "d"])
MAPPINGS = st.dictionaries(SEGMENTS, SEGMENTS, min_size=0, max_size=4)


def _resolve(mapping: dict[str, str]) -> int:
    """
    Run the park loop's bookkeeping, returning how many parks it took.

    Mirrors what ``_async_remap_segment`` does to ``mapping`` and
    ``pending`` when a pass stalls, without touching the registries.
    """
    mapping = dict(mapping)
    pending = set(mapping)
    parks = 0
    # Generous ceiling: a correct implementation parks at most once per
    # cycle, and there are fewer cycles than entries.
    while parks <= len(mapping) + 5:
        segment = _cycle_member(mapping, pending)
        if segment is None:
            return parks
        temp = _temporary_segment(mapping, set())
        mapping[temp] = mapping.pop(segment)
        pending.discard(segment)
        pending.add(temp)
        parks += 1
    raise AssertionError(f"did not terminate; mapping={mapping}")


@given(mapping=MAPPINGS)
@settings(max_examples=400)
def test_parking_always_terminates(mapping: dict[str, str]) -> None:
    """The park loop terminates for every mapping, cyclic or not.

    Both entry points are ``@callback``, so they run synchronously on the
    event loop -- a non-terminating mapping wedges Home Assistant outright.
    Via the migration it wedges it at *startup*, and because the version bump
    happens after the call, every subsequent restart wedges too.
    """
    _resolve(mapping)


@given(mapping=MAPPINGS)
def test_parking_is_bounded_by_the_number_of_entries(
    mapping: dict[str, str],
) -> None:
    """Each park breaks one cycle, so parks cannot exceed the entry count.

    A park that does not reduce the number of cycles is the shape both
    historical hangs took.
    """
    assert _resolve(mapping) <= len(mapping)


@given(mapping=MAPPINGS)
def test_a_reported_cycle_really_is_one(mapping: dict[str, str]) -> None:
    """Whatever ``_cycle_member`` returns must reach itself via the mapping.

    Parking a segment that is merely *chained* accomplishes nothing -- the
    second hang was exactly that.
    """
    segment = _cycle_member(mapping, set(mapping))
    if segment is None:
        return

    seen: set[str] = set()
    node = segment
    while node in mapping and node not in seen:
        seen.add(node)
        node = mapping[node]
    assert node == segment


@given(mapping=MAPPINGS)
def test_no_reported_cycle_means_the_mapping_is_acyclic(
    mapping: dict[str, str],
) -> None:
    """``None`` must mean no pending segment lies on a cycle.

    The converse of the property above: missing a real cycle leaves the rows
    stranded on names that belong to other users.
    """
    if _cycle_member(mapping, set(mapping)) is not None:
        return

    for start in mapping:
        seen: set[str] = set()
        node = start
        while node in mapping and node not in seen:
            seen.add(node)
            node = mapping[node]
        assert node != start


@given(mapping=MAPPINGS, in_use=st.sets(SEGMENTS, max_size=4))
def test_parking_name_never_collides(
    mapping: dict[str, str], in_use: set[str]
) -> None:
    """The parking name is free on both sides and in the registry.

    Moving onto an occupied name raises mid-pass, leaving identifiers half
    moved -- and a row can already sit on a parking name from a run that was
    interrupted.
    """
    candidate = _temporary_segment(mapping, in_use)

    assert candidate not in mapping
    assert candidate not in set(mapping.values())
    assert candidate not in in_use


@given(mapping=MAPPINGS)
def test_parking_preserves_where_each_segment_was_headed(
    mapping: dict[str, str],
) -> None:
    """A parked segment still ends up at the target it originally had.

    Parking is a detour, not a redirection. If it changed where a segment
    lands, one user's rows would arrive under another user's name -- which is
    exactly the cross-contamination that leaving cycles unresolved caused,
    reintroduced by the mechanism meant to fix it.
    """
    original = dict(mapping)
    working = dict(mapping)
    pending = set(working)
    # Which original segment each (possibly parked) key stands for.
    origin = {segment: segment for segment in working}

    for _ in range(len(working) + 5):
        segment = _cycle_member(working, pending)
        if segment is None:
            break
        temp = _temporary_segment(working, set())
        working[temp] = working.pop(segment)
        origin[temp] = origin.pop(segment)
        pending.discard(segment)
        pending.add(temp)

    for key, target in working.items():
        assert target == original[origin[key]]
