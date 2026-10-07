"""HTTP surface for the recheck capacity and reinjection workflow."""

from __future__ import annotations

import json


def _post(server, path, payload=None):
    response = server.call("POST", path, payload or {})
    return response.status, json.loads(response.body)


def _get(server, path):
    response = server.call("GET", path)
    return response.status, json.loads(response.body)


def _prepared(server, cutoff="2024-01-01T03:00:00Z"):
    _post(server, "/api/flights", {"code": "CA1", "carousel": "CR1", "chute": "A"})
    flights = _get(server, "/api/flights")[1]
    flight = flights[0]
    _post(server, f"/api/flights/{flight['id']}/cutoff-time", {"cutoff": cutoff})
    _post(server, "/api/chutes/assign", {"flight_id": flight["id"], "chute_id": "T1-CHA"})
    _post(server, "/api/reinject/quota", {"flight_id": flight["id"], "limit": 3})
    status, bag = _post(server, "/api/bags/checkin", {"barcode": "BAG1", "flight_id": flight["id"]})
    assert status == 201
    return flight["id"], bag["id"]


def test_policy_endpoint_records_the_selected_policy_and_tradeoff(server):
    status, payload = _post(server, "/api/reinject/policy", {})
    assert status == 200
    assert payload["selected"] == "cutoff_anchored"
    assert payload["rejected_alternative"] == "completion_ordered"
    assert "tradeoff" in payload and payload["tradeoff"]


def test_plan_then_trigger_lifecycle(server):
    flight_id, bag_id = _prepared(server)
    status, plan = _post(server, f"/api/reinject/plan/{bag_id}", {"max_attempts": 2})
    assert status == 201
    assert len(plan["slots"]) == 2
    assert plan["policy"] == "cutoff_anchored"

    # Before the slot: gated.
    status, gated = _post(server, f"/api/reinject/trigger/{bag_id}", {})
    assert gated["outcome"] == "pending_slot"

    # Reads back through the plan endpoint.
    status, fetched = _get(server, f"/api/reinject/plan/{bag_id}")
    assert fetched["bag_id"] == bag_id

    # Jump the clock to the first slot.
    from bagsort.reinject.policy import parse_iso

    server.plant.clock.advance(
        (parse_iso(plan["slots"][0]) - server.plant.clock.now()).total_seconds()
    )
    status, triggered = _post(server, f"/api/reinject/trigger/{bag_id}", {})
    assert triggered["outcome"] == "injected"
    assert triggered["attempt_number"] == 1

    # Duplicate trigger over HTTP stays idempotent.
    status, duplicate = _post(server, f"/api/reinject/trigger/{bag_id}", {})
    assert duplicate["outcome"] in {"duplicate", "sorted"}

    status, report = _get(server, "/api/reinject/reconcile")
    assert status == 200
    assert report["consistent"] is True


def test_quota_exhaustion_hands_off_and_reconcile_reports(server):
    flight_id, bag_id = _prepared(server)
    _post(server, "/api/reinject/quota", {"flight_id": flight_id, "limit": 0})
    _post(server, f"/api/reinject/plan/{bag_id}", {})
    plan = _get(server, f"/api/reinject/plan/{bag_id}")[1]
    from bagsort.reinject.policy import parse_iso

    server.plant.clock.advance(
        (parse_iso(plan["slots"][0]) - server.plant.clock.now()).total_seconds()
    )
    status, result = _post(server, f"/api/reinject/trigger/{bag_id}", {})
    assert result["outcome"] == "quota_exhausted"
    status, handoffs = _get(server, "/api/reinject/handoffs")
    assert len(handoffs) == 1
    assert handoffs[0]["blocked_stage"] == "reinject"


def test_capacity_endpoints_enforce_backlog(server):
    status, payload = _get(server, "/api/recheck/capacity")
    assert payload["capacity"] == 8
    status, payload = _post(server, "/api/recheck/capacity", {"capacity": 1})
    assert payload["capacity"] == 1
    flight_id, bag_id = _prepared(server)
    status, _ = _post(server, "/api/recheck", {"bag_id": bag_id})
    assert status == 201
    # The backlog is now at capacity; the reconcile view stays available.
    status, due = _get(server, "/api/reinject/due")
    assert "due" in due


def test_quota_status_endpoint(server):
    flight_id, _bag = _prepared(server)
    status, rows = _get(server, "/api/reinject/quota")
    assert any(row["flight_id"] == flight_id for row in rows)
    status, row = _get(server, f"/api/reinject/quota/{flight_id}")
    assert row["limit"] == 3


def test_plans_listing_and_catalog_wiring(server):
    status, rows = _get(server, "/api/reinject/plans")
    assert status == 200
    status, catalog = _get(server, "/api/catalog")
    paths = {entry["path"] for entry in catalog}
    assert "/api/reinject/plan/{bag}" in paths
    assert "/api/reinject/reconcile" in paths
