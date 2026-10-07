"""Resubmission control: slots, idempotency, escalation, throttling, reconcile."""

from __future__ import annotations

import json

import pytest

from bagsort.errors import ConflictError, QuotaError
from bagsort.model.entities import RetryPolicy
from bagsort.model.enums import BagState, RetryState
from bagsort.store.store import Store
from tests.support import check_in

CUTOFF_1H = "2024-01-01T01:00:00Z"


def fail(plant, bag):
    """Drive one bag through the verdict hook as a failed recheck."""

    plant.enqueue_recheck(bag.id)
    plant.process_recheck()
    return plant.fail_recheck(bag.id)


def test_slot_is_anchored_inside_the_cutoff_window(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bag = check_in(plant, flight.id, 1)[0]
    account = fail(plant, bag)
    assert account.state is RetryState.SCHEDULED
    # Default spacing is 300s and the window (cutoff - min cycle) is far away.
    assert account.slot == "2024-01-01T00:05:00Z"
    assert account.cutoff_at == CUTOFF_1H
    kinds = plant.store.kinds()
    assert kinds["retry.schedule"] == 1
    assert kinds["retry.policy"] == 1


def test_slot_without_cutoff_degenerates_to_plain_spacing(plant, flight):
    bag = check_in(plant, flight.id, 1)[0]
    account = fail(plant, bag)
    assert account.slot == "2024-01-01T00:05:00Z"
    assert account.cutoff_at == ""


def test_infeasible_window_escalates_to_manual(plant, flight):
    # cutoff in 180s: earliest slot (now + 300s) overshoots cutoff - 120s.
    plant.book.set_cutoff(flight.id, "2024-01-01T00:03:00Z")
    bag = check_in(plant, flight.id, 1)[0]
    account = fail(plant, bag)
    assert account.state is RetryState.MANUAL
    assert account.reason == "no_feasible_slot"
    assert account.stuck_stage == "recheck"
    assert plant.bag(bag.id).state is BagState.DIVERTED
    escalated = [
        entry for entry in plant.audit.for_subject(bag.id)
        if entry.kind == "retry_escalate"
    ]
    assert len(escalated) == 1
    assert escalated[0].detail["stage"] == "recheck"
    assert plant.audit.verify()


def test_attempts_exhausted_escalates_instead_of_dropping(plant, flight):
    plant.book.set_cutoff(flight.id, "2024-01-01T03:00:00Z")
    bag = check_in(plant, flight.id, 1)[0]
    for expected in (1, 2, 3):
        account = fail(plant, bag)
        assert account.state is RetryState.SCHEDULED
        plant.clock.advance(400)
        plant.run_due_retries()
        assert plant.retry.account(bag.id).attempts == expected
        assert plant.bag(bag.id).state is BagState.INJECTED
    account = fail(plant, bag)
    assert account.state is RetryState.MANUAL
    assert account.reason == "attempts_exhausted"
    assert account.attempts == 3
    assert plant.bag(bag.id).state is BagState.DIVERTED
    report = plant.reconcile_retries()
    assert report["ok"], report


def test_schedule_is_idempotent_under_repeated_triggers(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bag = check_in(plant, flight.id, 1)[0]
    first = fail(plant, bag)
    second = plant.fail_recheck(bag.id)
    assert first == second
    assert plant.store.kinds()["retry.schedule"] == 1
    scheduled = [
        entry for entry in plant.audit.for_subject(bag.id)
        if entry.kind == "retry_schedule"
    ]
    assert len(scheduled) == 1


def test_execute_is_idempotent_under_repeated_triggers(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bag = check_in(plant, flight.id, 1)[0]
    fail(plant, bag)
    # Not due yet: a trigger before the slot changes nothing.
    early = plant.execute_retry(bag.id)
    assert early.state is RetryState.SCHEDULED
    assert early.attempts == 0
    plant.clock.advance(300)
    plant.run_due_retries()
    plant.run_due_retries()
    again = plant.execute_retry(bag.id)
    assert again.attempts == 1
    assert plant.store.kinds()["retry.execute"] == 1
    assert plant.bag(bag.id).state is BagState.INJECTED
    report = plant.reconcile_retries()
    assert report["ok"], report
    assert report["attempts"] == 1
    assert report["executed_commands"] == 1


def test_execute_revalidates_the_window_and_escalates_a_lapsed_slot(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bag = check_in(plant, flight.id, 1)[0]
    fail(plant, bag)
    # The schedule moved: cutoff - min cycle is now 240s, before the slot.
    plant.revise_schedule(flight.id, "2024-01-01T00:06:00Z")
    plant.clock.advance(300)
    accounts = plant.run_due_retries()
    assert len(accounts) == 1
    assert accounts[0].state is RetryState.MANUAL
    assert accounts[0].reason == "slot_lapsed"
    assert plant.bag(bag.id).state is BagState.DIVERTED
    assert plant.store.kinds().get("retry.execute", 0) == 0


def test_closed_flight_escalates_instead_of_firing(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bag = check_in(plant, flight.id, 1)[0]
    fail(plant, bag)
    plant.clock.advance(400)
    plant.close_flight(flight.id)
    accounts = plant.run_due_retries()
    assert accounts[0].state is RetryState.MANUAL
    assert accounts[0].reason == "flight_closed"


def test_throttle_refuses_new_schedules_when_the_station_is_buried(plant, flight):
    plant.retry.register_policy(
        RetryPolicy(max_backlog=2, registered_at=plant.clock.now_iso())
    )
    bags = check_in(plant, flight.id, 3)
    for bag in bags:
        plant.enqueue_recheck(bag.id)
    with pytest.raises(QuotaError):
        plant.fail_recheck(bags[0].id)
    assert plant.store.state().counters["retry.throttled"] == 1


def test_escalation_is_not_throttled(plant, flight):
    # Escalation removes work from the station, so it must still go through
    # when the backlog is over the limit.
    plant.retry.register_policy(
        RetryPolicy(max_attempts=0, max_backlog=1, registered_at=plant.clock.now_iso())
    )
    bags = check_in(plant, flight.id, 2)
    for bag in bags:
        plant.enqueue_recheck(bag.id)
    account = plant.fail_recheck(bags[0].id)
    assert account.state is RetryState.MANUAL
    assert account.reason == "attempts_exhausted"


def test_backlog_pushes_the_slot_out(plant, flight):
    plant.retry.register_policy(
        RetryPolicy(service_time_seconds=60, registered_at=plant.clock.now_iso())
    )
    plant.book.set_cutoff(flight.id, "2024-01-01T02:00:00Z")
    bags = check_in(plant, flight.id, 10)
    for bag in bags:
        plant.enqueue_recheck(bag.id)
    account = plant.fail_recheck(bags[0].id)
    # spacing = max(300s interval, 10 bags x 60s service time) = 600s
    assert account.slot == "2024-01-01T00:10:00Z"


def test_scheduled_bag_leaves_the_recheck_queue(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bags = check_in(plant, flight.id, 2)
    for bag in bags:
        plant.enqueue_recheck(bag.id)
    plant.fail_recheck(bags[0].id)
    assert [bag.id for bag in plant.recheck.items()] == [bags[1].id]
    report = plant.reconcile_retries()
    assert report["ok"], report


def test_pass_clears_the_account_and_keeps_the_history(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bag = check_in(plant, flight.id, 1)[0]
    fail(plant, bag)
    plant.clock.advance(400)
    plant.run_due_retries()
    fail(plant, bag)
    account = plant.pass_recheck(bag.id)
    assert account.state is RetryState.CLEARED
    assert account.attempts == 1
    again = plant.pass_recheck(bag.id)
    assert again.state is RetryState.CLEARED
    assert plant.store.kinds()["retry.clear"] == 1
    report = plant.reconcile_retries()
    assert report["ok"], report


def test_retry_never_consumes_flight_allowance(plant, flight):
    plant.set_quota(flight.id, 10)
    bags = check_in(plant, flight.id, 3)
    assert plant.quota.available(flight.id) == 7
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    fail(plant, bags[0])
    plant.clock.advance(400)
    plant.run_due_retries()
    assert plant.quota.available(flight.id) == 7
    report = plant.reconcile_retries()
    assert report["ok"], report


def test_fail_requires_a_recheck_verdict(plant, flight):
    bag = check_in(plant, flight.id, 1)[0]
    with pytest.raises(ConflictError):
        plant.fail_recheck(bag.id)


def test_accounts_and_policy_survive_a_restart(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bag = check_in(plant, flight.id, 1)[0]
    fail(plant, bag)
    plant.store.persist()
    reopened = Store.open(plant.store.path)
    state = reopened.state()
    account = state.retry_accounts[bag.id]
    assert account.state is RetryState.SCHEDULED
    assert account.slot == "2024-01-01T00:05:00Z"
    assert state.retry_policy is not None
    assert state.retry_policy.strategy == "cutoff_anchored"
    # The trade-off is part of the committed record.
    assert "cutoff" in state.retry_policy.rationale
    assert "completion_order" in state.retry_policy.rejected


def test_audit_trail_covers_every_retry_transition(plant, flight):
    plant.book.set_cutoff(flight.id, CUTOFF_1H)
    bag = check_in(plant, flight.id, 1)[0]
    fail(plant, bag)
    plant.clock.advance(400)
    plant.run_due_retries()
    kinds = [
        entry.kind
        for entry in plant.audit.for_subject(bag.id)
        if entry.kind.startswith("retry_")
    ]
    assert kinds == ["retry_schedule", "retry_execute"]
    assert plant.audit.verify()


def test_retry_api(server):
    status, flight = _call(server, "POST", "/api/flights", {"code": "CA1234"})
    assert status == 201
    flight_id = flight["id"]
    _call(server, "POST", f"/api/flights/{flight_id}/cutoff-time", {"cutoff": CUTOFF_1H})
    status, bag = _call(
        server, "POST", "/api/bags/checkin", {"barcode": "BAG0001", "flight_id": flight_id}
    )
    assert status == 201
    _call(server, "POST", "/api/recheck", {"bag_id": bag["id"]})

    status, account = _call(server, "POST", "/api/recheck/retry/schedule", {"bag_id": bag["id"]})
    assert status == 201
    assert account["state"] == "scheduled"
    assert account["slot"] == "2024-01-01T00:05:00Z"

    status, fetched = _call(server, "GET", f"/api/recheck/retry/accounts/{bag['id']}")
    assert status == 200 and fetched["bag_id"] == bag["id"]
    status, accounts = _call(server, "GET", "/api/recheck/retry/accounts")
    assert len(accounts) == 1
    status, policy = _call(server, "GET", "/api/recheck/retry/policy")
    assert policy["strategy"] == "cutoff_anchored"
    assert policy["rationale"] and policy["rejected"]

    status, missing = _call(server, "GET", "/api/recheck/retry/accounts/nope")
    assert status == 404

    server.plant.clock.advance(400)
    status, fired = _call(server, "POST", "/api/recheck/retry/run")
    assert status == 200
    assert [item["state"] for item in fired] == ["in_transit"]
    assert fired[0]["attempts"] == 1

    status, report = _call(server, "GET", "/api/recheck/reconcile")
    assert status == 200 and report["ok"] is True
    assert report["attempts"] == 1


def _call(server, method, path, payload=None):
    response = server.call(method, path, payload)
    return response.status, json.loads(response.body)
