"""Reinjection director: planning, slot gate, idempotency, quota, handoff.

These tests encode the operating contract the request asks for:

* the system decides how many attempts and at which slots;
* attempts are idempotent per (bag, attempt);
* the attempt ledger agrees with the sort records and the quota ledger;
* bags past the limit go to manual handling with the blocked stage recorded;
* the recheck station applies explicit backpressure when its backlog is full.
"""

from __future__ import annotations

import pytest

from bagsort.errors import OrderingError, QuotaError
from bagsort.model.enums import BagState
from bagsort.reinject.policy import Policy, parse_iso
from bagsort.store.store import Store
from tests.support import check_in


CUTOFF = "2024-01-01T03:00:00Z"


def _ready_flight(plant, cutoff: str = CUTOFF):
    flight = plant.register_flight("CA1", "CR1", "A")
    plant.book.set_cutoff(flight.id, cutoff)
    plant.assign_chute(flight.id, "T1-CHA")
    return flight


def _failed_bag(plant, flight, policy_settings=None, quota=None):
    bag = check_in(plant, flight.id, 1)[0]
    if quota is not None:
        plant.reinject_quota.set_limit(flight.id, quota)
    plant.fail_recheck(bag.id, policy_settings)
    return plant.bag(bag.id)


def _advance_to(plant, slot: str) -> None:
    target = parse_iso(slot)
    plant.clock.advance((target - plant.clock.now()).total_seconds())


def test_plan_decides_count_and_cutoff_anchored_slots(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight, Policy(max_attempts=2, interval_seconds=60, guard_seconds=120))
    plan = plant.reinject.plan_for(bag.id)
    assert plan.status == "scheduled"
    assert plan.max_attempts == 2
    # Last slot sits on cutoff minus guard; previous slot one interval earlier.
    assert plan.slots[-1] == "2024-01-01T02:58:00Z"
    assert plan.slots[-2] == "2024-01-01T02:57:00Z"
    assert plan.policy == "cutoff_anchored"
    assert plan.rejected_alternative == "completion_ordered"
    assert plan.tradeoff  # the tradeoff is persisted on the plan
    assert bag.state is BagState.REINJECTING


def test_trigger_before_the_slot_is_a_gate_not_an_injection(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight)
    result = plant.trigger_reinjection(bag.id)
    assert result.outcome == "pending_slot"
    assert result.injected is False
    assert plant.sorter.log.reinjection_count(bag.id) == 0


def test_trigger_at_the_slot_injects_and_debits_quota(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight, quota=3)
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    result = plant.trigger_reinjection(bag.id)
    assert result.outcome == "injected"
    assert result.attempt_number == 1
    assert plant.bag(bag.id).state is BagState.SORTED
    assert [record.attempt for record in plant.sorter.records()] == [1]
    assert plant.reinject_quota.available(flight.id) == 2
    assert plant.reinject.used(bag.id) == 1


def test_repeated_trigger_is_idempotent_one_injection_one_record(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight, quota=3)
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    first = plant.trigger_reinjection(bag.id)
    second = plant.trigger_reinjection(bag.id)
    third = plant.trigger_reinjection(bag.id)
    assert first.outcome == "injected"
    assert second.outcome in {"duplicate", "sorted"}
    assert third.outcome in {"duplicate", "sorted"}
    assert plant.sorter.log.reinjection_count(bag.id) == 1
    assert plant.reinject_quota.available(flight.id) == 2
    assert len(plant.reinject.plan_for(bag.id).attempts) == 1


def test_failing_again_reschedules_then_hands_off_after_the_limit(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight, Policy(max_attempts=2), quota=5)
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    plant.trigger_reinjection(bag.id)

    resumed = plant.report_recheck_failure(bag.id, "still unreadable")
    assert resumed.status == "scheduled"
    assert resumed.used_attempts == 1
    assert plant.bag(bag.id).state is BagState.REINJECTING

    _advance_to(plant, resumed.slots[1])
    plant.trigger_reinjection(bag.id)
    closed = plant.report_recheck_failure(bag.id)
    assert closed.status == "handoff"

    handoff = plant.reinject.handoff_for(bag.id)
    assert handoff is not None
    assert handoff.attempts_used == 2
    assert handoff.max_attempts == 2
    assert handoff.blocked_stage == "recheck"
    assert "limit" in handoff.reason
    assert plant.bag(bag.id).state is BagState.MANUAL_HANDLING


def test_handoff_is_a_visible_ledger_entry_not_a_silent_drop(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight)
    plant.handoff_bag(bag.id, "operator escalated", blocked_stage="recheck")
    assert len(plant.reinject.handoffs()) == 1
    assert plant.bag(bag.id).state is BagState.MANUAL_HANDLING
    assert plant.reinject.plan_for(bag.id).status == "handoff"


def test_exhausted_quota_blocks_the_injection_and_hands_off(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight, quota=0)
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    result = plant.trigger_reinjection(bag.id)
    assert result.outcome == "quota_exhausted"
    assert result.plan_status == "handoff"
    handoff = plant.reinject.handoff_for(bag.id)
    assert handoff.blocked_stage == "reinject"
    assert "allowance" in handoff.reason
    assert plant.sorter.log.reinjection_count(bag.id) == 0


def test_missing_chute_blocks_at_the_chute_stage(plant):
    flight = plant.register_flight("CA1", "CR1", "")
    plant.book.set_cutoff(flight.id, CUTOFF)
    bag = check_in(plant, flight.id, 1)[0]
    plant.fail_recheck(bag.id)
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    result = plant.trigger_reinjection(bag.id)
    assert result.outcome == "no_chute"
    assert plant.reinject.handoff_for(bag.id).blocked_stage == "chute"


def test_closed_flight_blocks_and_hands_off(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight)
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    plant.book.mark_closed(flight.id)
    result = plant.trigger_reinjection(bag.id)
    assert result.outcome == "flight_closed"
    assert plant.reinject.handoff_for(bag.id).blocked_stage == "reinject"


def test_passing_the_guard_deadline_hands_off_without_a_post_cutoff_injection(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(
        plant, flight, Policy(max_attempts=1, interval_seconds=60, guard_seconds=300)
    )
    plan = plant.reinject.plan_for(bag.id)
    # Skip straight past the guarded deadline (cutoff minus 5 minutes).
    _advance_to(plant, "2024-01-01T02:56:00Z")
    result = plant.trigger_reinjection(bag.id)
    assert result.outcome == "past_deadline"
    assert plant.sorter.log.reinjection_count(bag.id) == 0
    assert plant.reinject.handoff_for(bag.id) is not None
    assert plan.slots == ["2024-01-01T02:55:00Z"]


def test_no_cutoff_means_immediate_manual_handoff(plant):
    flight = plant.register_flight("CA1", "CR1", "A")
    plant.assign_chute(flight.id, "T1-CHA")
    bag = check_in(plant, flight.id, 1)[0]
    plan = plant.fail_recheck(bag.id)
    assert plan.status == "handoff"
    handoff = plant.reinject.handoff_for(bag.id)
    assert "cutoff" in handoff.reason
    assert plant.bag(bag.id).state is BagState.MANUAL_HANDLING


def test_due_lists_bags_in_slot_order_and_trigger_due_is_bounded(plant):
    flight = _ready_flight(plant)
    bags = []
    plant.clock.advance(10)
    for index in range(3):
        bag = check_in(plant, flight.id, 1, prefix=f"BAG{index}")[0]
        plant.fail_recheck(bag.id, Policy(max_attempts=1, interval_seconds=60, guard_seconds=60))
        bags.append(bag)
        plant.clock.advance(5)
    # Nothing due yet.
    assert plant.reinject.due() == []
    due_after = [plan.slots[0] for plan in plant.reinject.plans()]
    _advance_to(plant, max(due_after))
    due = plant.reinject.due()
    assert len(due) == 3
    results = plant.trigger_due_reinjections()
    assert all(result.outcome == "injected" for result in results)
    # Sweeping again injects nothing: the slot gate keeps the batch bounded.
    assert plant.trigger_due_reinjections() == []


def test_reconcile_cross_checks_ledger_sort_records_and_handoff_state(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight, quota=2)
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    plant.trigger_reinjection(bag.id)
    report = plant.reinjection_reconcile()
    assert report["consistent"] is True
    assert report["injected"] == 1
    assert report["handoffs"] == 0


def test_trigger_requires_an_existing_plan(plant):
    flight = _ready_flight(plant)
    bag = check_in(plant, flight.id, 1)[0]
    with pytest.raises(OrderingError):
        plant.trigger_reinjection(bag.id)


def test_plan_and_handoff_survive_restart(plant, tmp_path):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight, quota=2)
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    plant.trigger_reinjection(bag.id)
    plant.report_recheck_failure(bag.id)
    plant.store.persist()
    reopened = Store.open(plant.store.path)
    plan_state = reopened.state().reinject_plans[bag.id]
    assert plan_state.status == "scheduled"
    assert plan_state.used_attempts == 1
    assert plan_state.tradeoff
    assert reopened.state().reinject_limits[flight.id] == 1
    assert len(reopened.state().manual_handoffs) == 0


def test_every_decision_is_in_the_audit_chain(plant):
    flight = _ready_flight(plant)
    bag = _failed_bag(plant, flight)
    subjects = [entry.kind for entry in plant.audit.for_subject(bag.id)]
    assert "reinject_plan" in subjects
    assert plant.audit.verify() is True
    plan = plant.reinject.plan_for(bag.id)
    _advance_to(plant, plan.slots[0])
    plant.trigger_reinjection(bag.id)
    assert "reinjection" in [entry.kind for entry in plant.audit.for_subject(bag.id)]
    assert plant.audit.verify() is True
