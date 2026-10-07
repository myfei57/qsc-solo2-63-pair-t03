"""Explicit rate limiting when the recheck station backlog is over capacity."""

from __future__ import annotations

import pytest

from bagsort.errors import QuotaError
from tests.support import check_in


def _flight(plant):
    flight = plant.register_flight("CA1", "CR1", "A")
    plant.book.set_cutoff(flight.id, "2024-01-01T03:00:00Z")
    plant.assign_chute(flight.id, "T1-CHA")
    return flight


def test_default_station_capacity_limits_the_backlog(plant):
    assert plant.reinject.recheck_capacity() == 8
    flight = _flight(plant)
    bags = check_in(plant, flight.id, 8)
    for bag in bags:
        plant.enqueue_recheck(bag.id)
    ninth = check_in(plant, flight.id, 1, prefix="OVR")[0]
    with pytest.raises(QuotaError):
        plant.enqueue_recheck(ninth.id)


def test_capacity_zero_means_unlimited(plant):
    plant.set_recheck_capacity(0)
    flight = _flight(plant)
    bags = check_in(plant, flight.id, 12)
    for bag in bags:
        plant.enqueue_recheck(bag.id)
    assert plant.recheck.length() == 12


def test_processing_a_bag_frees_capacity(plant):
    plant.set_recheck_capacity(1)
    flight = _flight(plant)
    first = check_in(plant, flight.id, 1, prefix="A")[0]
    plant.enqueue_recheck(first.id)
    second = check_in(plant, flight.id, 1, prefix="B")[0]
    with pytest.raises(QuotaError):
        plant.enqueue_recheck(second.id)
    plant.process_recheck()
    plant.enqueue_recheck(second.id)  # admitted once the station drains
    assert plant.recheck.length() == 1


def test_capacity_change_is_committed_and_survives_restart(plant):
    plant.set_recheck_capacity(3)
    plant.store.persist()
    from bagsort.store.store import Store

    reopened = Store.open(plant.store.path)
    assert reopened.state().recheck_capacity == 3


def test_negative_capacity_is_rejected(plant):
    with pytest.raises(QuotaError):
        plant.set_recheck_capacity(-1)


def test_failed_recheck_admission_respects_the_same_capacity(plant):
    plant.set_recheck_capacity(1)
    flight = _flight(plant)
    first = check_in(plant, flight.id, 1, prefix="A")[0]
    plant.enqueue_recheck(first.id)
    second = check_in(plant, flight.id, 1, prefix="B")[0]
    # admit checks the physical backlog before committing a reinjection plan.
    with pytest.raises(QuotaError):
        plant.fail_recheck(second.id)
    assert plant.reinject.plan_for(second.id) is None
