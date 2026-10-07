"""Per-flight reinjection allowance ledger."""

from __future__ import annotations

import pytest

from bagsort.errors import ValidationError
from bagsort.reinject.quota import UNLIMITED
from tests.support import check_in


def _flight(plant):
    flight = plant.register_flight("CA1", "CR1", "A")
    plant.book.set_cutoff(flight.id, "2024-01-01T03:00:00Z")
    plant.assign_chute(flight.id, "T1-CHA")
    return flight


def test_unlimited_by_default(plant):
    flight = _flight(plant)
    assert plant.reinject_quota.limit(flight.id) == UNLIMITED
    assert plant.reinject_quota.consume(flight.id, 99) is True


def test_consume_and_refuse_when_exhausted(plant):
    flight = _flight(plant)
    plant.reinject_quota.set_limit(flight.id, 1)
    assert plant.reinject_quota.consume(flight.id) is True
    assert plant.reinject_quota.allowed(flight.id) is False
    assert plant.reinject_quota.consume(flight.id) is False


def test_release_returns_capacity(plant):
    flight = _flight(plant)
    plant.reinject_quota.set_limit(flight.id, 1)
    plant.reinject_quota.consume(flight.id)
    assert plant.reinject_quota.release(flight.id) == 1


def test_status_rows(plant):
    flight = _flight(plant)
    plant.reinject_quota.set_limit(flight.id, 2)
    assert plant.reinject_quota.status() == [
        {"flight_id": flight.id, "limit": 2, "unlimited": False, "allowed": True}
    ]


def test_invalid_limits_are_rejected(plant):
    flight = _flight(plant)
    with pytest.raises(ValidationError):
        plant.reinject_quota.set_limit(flight.id, -2)
    with pytest.raises(ValidationError):
        plant.reinject_quota.set_limit("", 1)
    with pytest.raises(ValidationError):
        plant.reinject_quota.consume(flight.id, -1)


def test_ledger_agrees_with_injected_sort_records(plant):
    """One debit per committed reinjection, zero for gated/blocked triggers."""

    from bagsort.reinject.policy import parse_iso

    flight = _flight(plant)
    plant.reinject_quota.set_limit(flight.id, 3)
    bag = check_in(plant, flight.id, 1)[0]
    plant.fail_recheck(bag.id)
    plan = plant.reinject.plan_for(bag.id)

    # Pending slot spends nothing.
    plant.trigger_reinjection(bag.id)
    assert plant.reinject_quota.available(flight.id) == 3

    plant.clock.advance((parse_iso(plan.slots[0]) - plant.clock.now()).total_seconds())
    plant.trigger_reinjection(bag.id)
    plant.trigger_reinjection(bag.id)  # idempotent duplicate
    assert plant.reinject_quota.available(flight.id) == 2
    assert plant.sorter.log.reinjection_count(bag.id) == 1
    assert plant.reinjection_reconcile()["consistent"] is True
