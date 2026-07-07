"""Pure unit tests for app.curriculum.segment.partition_by_minutes: no DB, no
model - just the greedy bin-packing/pacing logic.

Mirrors test_chunk.py's style (pure-function unit tests for a partitioning
helper, generous fixture data, invariant-style assertions) and test_retrieve.
py's convention of using plain strings for UUID-typed dataclass fields
(block_id) in tests that never touch the DB.
"""
import pytest

from app.curriculum.segment import Leaf, partition_by_minutes


def _leaf(n: int, minutes: int, module: str | None = "M1") -> Leaf:
    return Leaf(block_id=f"leaf-{n}", minutes=minutes, title=f"Leaf {n}", module_title=module)


def _sum(session: list[Leaf]) -> int:
    return sum(leaf.minutes for leaf in session)


def test_empty_leaves_yields_no_sessions():
    assert partition_by_minutes([], 50) == []


def test_non_positive_session_minutes_raises():
    with pytest.raises(ValueError):
        partition_by_minutes([_leaf(1, 10)], 0)
    with pytest.raises(ValueError):
        partition_by_minutes([_leaf(1, 10)], -5)


def test_uniform_leaves_pack_to_bounds_and_preserve_order():
    """10 same-module 15-min leaves at target=50 (cap=60, floor=25): every
    4th leaf busts the 60-min cap, so this yields [60, 60, 30]-minute
    sessions - all within [0.5x, 1.2x] of the 50-min target - and every leaf
    ends up in exactly one session, in original order.
    """
    leaves = [_leaf(i, 15) for i in range(10)]

    sessions = partition_by_minutes(leaves, 50)

    assert [_sum(s) for s in sessions] == [60, 60, 30]
    for s in sessions:
        assert 25 <= _sum(s) <= 60  # [0.5x, 1.2x] of target=50

    flattened = [leaf for s in sessions for leaf in s]
    assert flattened == leaves  # order preserved, nothing dropped or duplicated


def test_module_boundary_preferred_when_current_session_already_meets_floor():
    """Module A's two leaves alone already reach the 0.5x-target floor
    (30 >= 25 for target=50); the next leaf belongs to Module B and would
    still fit under the cap (30+10=40 <= 60) - but the boundary preference
    should close the session anyway rather than mix modules once a session
    is already "big enough".
    """
    leaves = [
        _leaf(1, 15, "Module A"), _leaf(2, 15, "Module A"),  # 30 min, >= floor
        _leaf(3, 10, "Module B"), _leaf(4, 10, "Module B"),
    ]

    sessions = partition_by_minutes(leaves, 50)

    assert len(sessions) == 2
    assert sessions[0] == leaves[:2]  # Module A's two leaves, alone - the boundary held
    assert sessions[1] == leaves[2:]  # Module B's leaves start a fresh session


def test_module_boundary_ignored_when_it_would_leave_a_too_small_session():
    """Module A contributes only 10 minutes (< the 25-min floor) before
    Module B starts - closing right at the boundary would strand a
    10-minute session, so packing must continue across the boundary instead.
    """
    leaves = [
        _leaf(1, 10, "Module A"),
        _leaf(2, 15, "Module B"), _leaf(3, 15, "Module B"),
    ]

    sessions = partition_by_minutes(leaves, 50)

    assert len(sessions) == 1  # 10+15+15=40 <= cap(60); packed straight through
    assert sessions[0] == leaves


def test_single_oversized_leaf_becomes_its_own_session():
    """A 200-min leaf against a 50-min target (cap=60) is already over cap
    alone; a leaf is never split, so it must be isolated as its own session,
    while its neighbours still pack normally on either side.
    """
    leaves = [
        _leaf(1, 20, "Module A"),
        _leaf(2, 200, "Module A"),
        _leaf(3, 20, "Module B"),
    ]

    sessions = partition_by_minutes(leaves, 50)

    assert sessions == [[leaves[0]], [leaves[1]], [leaves[2]]]
    assert _sum(sessions[1]) == 200


def test_single_oversized_leaf_alone_in_input_is_its_own_session():
    """The minimal case named directly in the brief: one 200-min leaf, no
    neighbours, target=50 -> a single session containing just that leaf.
    """
    leaves = [_leaf(1, 200, "Module A")]

    sessions = partition_by_minutes(leaves, 50)

    assert sessions == [leaves]


def test_realistic_course_yields_expected_session_count_range():
    """~1560 minutes spread over 6 modules (varied lesson lengths, like a
    real generated course) at a 50-min target should land in the same
    ~25-35 session ballpark the brief's integration test expects for a
    ~1500-minute course - entirely without a DB or model. A larger target on
    the same leaves must yield fewer, longer sessions.
    """
    lesson_lengths = [45, 30, 20, 15, 40, 25, 35, 50]  # 260 min/module
    leaves = [
        _leaf(m * 100 + i, minutes, f"Module {m}")
        for m in range(6)
        for i, minutes in enumerate(lesson_lengths)
    ]
    total = sum(leaf.minutes for leaf in leaves)
    assert total == 1560

    sessions = partition_by_minutes(leaves, 50)

    assert 24 <= len(sessions) <= 36
    assert sum(_sum(s) for s in sessions) == total  # every minute accounted for exactly once
    flattened = [leaf for s in sessions for leaf in s]
    assert flattened == leaves  # order preserved across the whole course

    fewer_sessions = partition_by_minutes(leaves, 120)
    assert len(fewer_sessions) < len(sessions)
    assert sum(_sum(s) for s in fewer_sessions) == total
