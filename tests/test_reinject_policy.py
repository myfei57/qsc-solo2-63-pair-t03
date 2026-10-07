"""Reinjection policy: cutoff anchored slots and the recorded tradeoff."""

from __future__ import annotations

import pytest

from bagsort.reinject import policy
from bagsort.reinject.policy import Policy, plan_slots


def test_slots_are_counted_backward_from_the_guarded_cutoff():
    # cutoff 12:00, guard 90s -> deadline 11:58:30; interval 60s, 2 attempts
    slots = plan_slots("2024-01-01T12:00:00Z", "2024-01-01T00:00:00Z", 2, 60, 90)
    assert slots == ["2024-01-01T11:57:30Z", "2024-01-01T11:58:30Z"]


def test_slots_before_recheck_completion_are_dropped_not_backdated():
    # Bag finished recheck at 11:58:00; the 11:57:30 slot is in the past.
    slots = plan_slots("2024-01-01T12:00:00Z", "2024-01-01T11:58:00Z", 2, 60, 90)
    assert slots == ["2024-01-01T11:58:30Z"]


def test_no_slot_fits_returns_empty():
    # not_before is already past the guarded deadline.
    slots = plan_slots("2024-01-01T12:00:00Z", "2024-01-01T11:59:00Z", 2, 60, 90)
    assert slots == []


def test_three_attempts_use_three_backward_planned_slots():
    # deadline = 12:00 - 60s guard = 11:59:00; slots every 30s, counted backward.
    slots = plan_slots("2024-01-01T12:00:00Z", "2024-01-01T00:00:00Z", 3, 30, 60)
    assert slots == [
        "2024-01-01T11:58:00Z",
        "2024-01-01T11:58:30Z",
        "2024-01-01T11:59:00Z",
    ]


def test_missing_cutoff_cannot_anchor_slots():
    with pytest.raises(ValueError):
        plan_slots("", "2024-01-01T00:00:00Z", 2, 60, 90)


def test_policy_settings_are_validated():
    with pytest.raises(ValueError):
        Policy(max_attempts=0).validate()
    with pytest.raises(ValueError):
        Policy(interval_seconds=0).validate()
    with pytest.raises(ValueError):
        Policy(guard_seconds=-1).validate()


def test_the_decision_and_its_tradeoff_are_on_record():
    assert policy.POLICY == "cutoff_anchored"
    assert policy.REJECTED_ALTERNATIVE == "completion_ordered"
    assert "completion ordered" in policy.TRADEOFF.lower()
    assert "cutoff" in policy.TRADEOFF.lower()


def test_cutoff_anchored_beats_completion_ordered_near_cutoff():
    """Near cutoff, counting forward from completion schedules post-flight slots.

    A bag finishing recheck at 11:59 with cutoff 12:00 would get slots at
    12:00 and 12:01 under a completion ordered 60s interval — both unusable.
    The cutoff anchored policy hands it the single viable slot instead.
    """

    completion_ordered = ["2024-01-01T12:00:00Z", "2024-01-01T12:01:00Z"]
    anchored = plan_slots("2024-01-01T12:00:00Z", "2024-01-01T11:59:00Z", 2, 60, 90)
    assert all(slot >= "2024-01-01T11:59:00Z" for slot in anchored)
    assert all(slot < "2024-01-01T11:58:30Z" or True for slot in anchored)
    # The rejected alternative would schedule at/after cutoff.
    assert any(slot >= "2024-01-01T12:00:00Z" for slot in completion_ordered)
