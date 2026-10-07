"""Decides and executes reinjections after a failed recheck.

This is the module that replaces "re-queue and shout on the floor". Given a bag
that failed recheck it decides:

* how many reinjection attempts remain (bag cap intersected with the flight's
  reinjection allowance),
* at which instants they happen (see :mod:`bagsort.reinject.policy`),
* and, when nothing viable remains, which manual-handling queue receives the
  bag and at which stage it got stuck.

Everything decided or done here is a committed command: plans, attempts,
allowance movements and manual handoffs all land in the journal and the audit
chain. Executing an attempt is idempotent on the deterministic key
``bag_id#attempt``; retriggering the same attempt returns the recorded attempt
unchanged and never produces a second sort record.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.chute.assigner import ChuteAssigner
from bagsort.clock import Clock
from bagsort.errors import ConflictError, OrderingError, QuotaError
from bagsort.flight.book import FlightBook
from bagsort.model.entities import (
    Bag,
    ManualHandoff,
    Reading,
    ReinjectAttempt,
    ReinjectPlan,
)
from bagsort.model.enums import BagState, ReinjectOutcome
from bagsort.model.errors import unknown_bag
from bagsort.model.validate import is_open_state
from bagsort.reinject import policy
from bagsort.reinject.quota import ReinjectQuota
from bagsort.sorter.sorter import Sorter
from bagsort.store.store import Store


def attempt_key(bag_id: str, attempt_number: int) -> str:
    return f"{bag_id}#{attempt_number}"


@dataclass(frozen=True)
class TriggerResult:
    """What one ``trigger`` call did; duplicate triggers report ``duplicate``."""

    bag_id: str
    attempt_number: int
    outcome: str
    injected: bool
    plan_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "bag_id": self.bag_id,
            "attempt_number": self.attempt_number,
            "outcome": self.outcome,
            "injected": self.injected,
            "plan_status": self.plan_status,
        }


class ReinjectDirector:
    """Plans, times and executes reinjections, handing the rest to manual care."""

    def __init__(
        self,
        store: Store,
        book: FlightBook,
        chutes: ChuteAssigner,
        sorter: Sorter,
        quota: ReinjectQuota,
        clock: Clock,
    ) -> None:
        self._store = store
        self._book = book
        self._chutes = chutes
        self._sorter = sorter
        self._quota = quota
        self._clock = clock

    # -- station backlog limit --------------------------------------------

    def set_recheck_capacity(self, capacity: int) -> int:
        """Maximum backlog the recheck station accepts; 0 disables the limit."""

        if capacity < 0:
            raise QuotaError("recheck capacity must not be negative")
        with self._store.session() as session:
            session.state.recheck_capacity = capacity
            session.record(
                "recheck.capacity",
                {"capacity": capacity},
                self._clock.now_iso(),
            )
        return capacity

    def recheck_capacity(self) -> int:
        return self._store.state().recheck_capacity

    def has_capacity(self) -> bool:
        capacity = self.recheck_capacity()
        return capacity == 0 or len(self._store.state().recheck_queue) < capacity

    # -- planning ----------------------------------------------------------

    def admit(self, bag: Bag, settings: policy.Policy | None = None) -> ReinjectPlan:
        """Enforce the station backlog limit, then plan a failed-recheck bag."""

        if not self.has_capacity():
            raise QuotaError(
                f"recheck station backlog is at capacity {self.recheck_capacity()}"
            )
        return self.plan(bag, settings)

    def plan(self, bag: Bag, settings: policy.Policy | None = None) -> ReinjectPlan:
        """Compute and commit the reinjection plan for a recheck-failed bag."""

        chosen = (settings or policy.Policy()).validate()
        existing = self._store.state().reinject_plans.get(bag.id)
        if existing is not None and existing.status in {"scheduled", "rechecking"}:
            raise ConflictError(f"bag {bag.id!r} already has an active reinjection plan")

        flight = self._book.get(bag.flight_id)
        now_iso = self._clock.now_iso()
        if not flight.cutoff_at:
            return self._handoff(
                bag,
                reason="flight has no cutoff on record; reinjection cannot be scheduled",
                blocked_stage="recheck",
                settings=chosen,
            )

        slots = policy.plan_slots(
            flight.cutoff_at,
            not_before=now_iso,
            max_attempts=chosen.max_attempts,
            interval_seconds=chosen.interval_seconds,
            guard_seconds=chosen.guard_seconds,
        )
        if not slots:
            return self._handoff(
                bag,
                reason=(
                    f"no reinjection slot before cutoff {flight.cutoff_at} "
                    f"(guard {chosen.guard_seconds}s)"
                ),
                blocked_stage="recheck",
                settings=chosen,
                cutoff_at=flight.cutoff_at,
                schedule_generation=flight.schedule_generation,
            )

        plan = ReinjectPlan(
            bag_id=bag.id,
            flight_id=bag.flight_id,
            max_attempts=chosen.max_attempts,
            interval_seconds=chosen.interval_seconds,
            guard_seconds=chosen.guard_seconds,
            cutoff_at=flight.cutoff_at,
            schedule_generation=flight.schedule_generation,
            slots=slots,
            status="scheduled",
            policy=policy.POLICY,
            rejected_alternative=policy.REJECTED_ALTERNATIVE,
            tradeoff=policy.TRADEOFF,
            created_at=now_iso,
        )
        updated = bag.evolved(state=BagState.REINJECTING)
        with self._store.session() as session:
            session.state.reinject_plans[bag.id] = plan
            session.state.bags[updated.id] = updated
            session.record("reinject.plan.put", {"plan": plan.as_dict()}, now_iso)
            session.record("bag.put", {"bag": updated.as_dict()}, now_iso)
            session.record("counter.add", {"name": "reinject.planned", "delta": 1}, now_iso)
        return plan

    # -- execution ---------------------------------------------------------

    def trigger(self, bag_id: str) -> TriggerResult:
        """Fire the bag's next due attempt, or report why nothing was injected."""

        plan = self.require_plan(bag_id)
        if plan.status in {"sorted", "handoff"}:
            return TriggerResult(bag_id, 0, plan.status, False, plan.status)

        next_number = plan.used_attempts + 1
        if next_number > len(plan.slots) or next_number > plan.max_attempts:
            self._handoff(
                self._bag(bag_id),
                reason=f"reinjection limit of {plan.max_attempts} attempts reached",
                blocked_stage="reinject",
                plan=plan,
            )
            return TriggerResult(
                bag_id, next_number, ReinjectOutcome.HANDOFF.value, False, "handoff"
            )

        slot_at = plan.slots[next_number - 1]
        key = attempt_key(bag_id, next_number)
        if self._find_attempt(plan, key) is not None:
            # Duplicate trigger for an already executed attempt: no second write.
            return TriggerResult(
                bag_id, next_number, ReinjectOutcome.DUPLICATE.value, False, plan.status
            )

        if policy.parse_iso(self._clock.now_iso()) < policy.parse_iso(slot_at):
            return TriggerResult(
                bag_id, next_number, ReinjectOutcome.PENDING_SLOT.value, False, plan.status
            )

        block = self._block_reason(plan)
        if block is not None:
            outcome, reason, stage = block
            self._handoff(
                self._bag(bag_id),
                reason=reason,
                blocked_stage=stage,
                plan=plan,
                blocking=(key, next_number, slot_at, outcome, reason),
            )
            return TriggerResult(bag_id, next_number, outcome.value, False, "handoff")

        bag = self._bag(bag_id)
        reading = Reading(
            barcode=bag.barcode,
            bag_id=bag.id,
            sequence=bag.sequence,
            epoch=bag.epoch,
            committed=True,
        )
        at = self._clock.now_iso()
        with self._store.session() as session:
            # Debit the allowance, write the sort record and the attempt ledger
            # in one session: any failure rolls all three back together, so the
            # quota balance can never drift from the sort records.
            if not self._quota.apply_consume(session, plan.flight_id):
                raise ConflictError("reinjection allowance vanished under the trigger")
            record = self._sorter.apply_attempt(session, bag, reading, next_number)
            attempt = ReinjectAttempt(
                key=key,
                bag_id=bag_id,
                attempt_number=next_number,
                slot_at=slot_at,
                outcome=ReinjectOutcome.INJECTED.value,
                at=at,
                sort_record_at=record.at,
                chute_id=record.chute_id,
            )
            updated_plan = plan.evolved(
                attempts=[*plan.attempts, attempt], status="sorted"
            )
            session.state.reinject_plans[bag_id] = updated_plan
            session.record(
                "reinject.plan.put", {"plan": updated_plan.as_dict()}, at
            )
        return TriggerResult(
            bag_id, next_number, ReinjectOutcome.INJECTED.value, True, "sorted"
        )

    def due(self) -> list[str]:
        """Bags whose next scheduled slot has arrived, in slot order."""

        now = policy.parse_iso(self._clock.now_iso())
        ready: list[tuple[str, str]] = []
        for plan in self._store.state().reinject_plans.values():
            if plan.status != "scheduled":
                continue
            next_number = plan.used_attempts + 1
            if next_number > len(plan.slots):
                continue
            slot_at = plan.slots[next_number - 1]
            if now >= policy.parse_iso(slot_at):
                ready.append((slot_at, plan.bag_id))
        return [bag_id for _slot, bag_id in sorted(ready)]

    def trigger_due(self) -> list[TriggerResult]:
        """Fire every due attempt once; the station's bounded batch."""

        return [self.trigger(bag_id) for bag_id in self.due()]

    def fail_current(self, bag_id: str, reason: str = "recheck failed again") -> ReinjectPlan:
        """A re-injected bag failed recheck once more: reschedule or hand off."""

        plan = self.require_plan(bag_id)
        used = plan.used_attempts
        if used >= len(plan.slots) or used >= plan.max_attempts:
            return self._handoff(
                self._bag(bag_id),
                reason=(
                    f"reinjection limit of {plan.max_attempts} attempts reached: {reason}"
                ),
                blocked_stage="recheck",
                plan=plan,
            )
        resumed = plan.evolved(status="scheduled")
        updated = self._bag(bag_id).evolved(state=BagState.REINJECTING)
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.reinject_plans[bag_id] = resumed
            session.state.bags[bag_id] = updated
            session.record("reinject.plan.put", {"plan": resumed.as_dict()}, at)
            session.record("bag.put", {"bag": updated.as_dict()}, at)
            session.record(
                "counter.add", {"name": "reinject.rescheduled", "delta": 1}, at
            )
        return resumed

    # -- manual handling ---------------------------------------------------

    def handoff(self, bag_id: str, reason: str, blocked_stage: str = "reinject") -> ManualHandoff:
        """Explicit operator handoff for a bag that the loop will not resolve."""

        return self._handoff(self._bag(bag_id), reason, blocked_stage)

    def handoffs(self) -> list[ManualHandoff]:
        return list(self._store.state().manual_handoffs)

    def handoff_for(self, bag_id: str) -> ManualHandoff | None:
        for handoff in reversed(self._store.state().manual_handoffs):
            if handoff.bag_id == bag_id:
                return handoff
        return None

    # -- read side ---------------------------------------------------------

    def plan_for(self, bag_id: str) -> ReinjectPlan | None:
        return self._store.state().reinject_plans.get(bag_id)

    def require_plan(self, bag_id: str) -> ReinjectPlan:
        plan = self.plan_for(bag_id)
        if plan is None:
            raise OrderingError(f"bag {bag_id!r} has no reinjection plan")
        return plan

    def plans(self) -> list[ReinjectPlan]:
        return [
            self._store.state().reinject_plans[key]
            for key in sorted(self._store.state().reinject_plans)
        ]

    def attempt(self, bag_id: str, attempt_number: int) -> ReinjectAttempt | None:
        plan = self.plan_for(bag_id)
        if plan is None:
            return None
        return self._find_attempt(plan, attempt_key(bag_id, attempt_number))

    def used(self, bag_id: str) -> int:
        plan = self.plan_for(bag_id)
        return plan.used_attempts if plan else 0

    def reconcile(self) -> dict[str, object]:
        """Cross-check the recheck queue, sort records and reinjection ledgers."""

        state = self._store.state()
        discrepancies: list[str] = []
        for plan in state.reinject_plans.values():
            sort_rows = self._sorter.log.reinjection_count(plan.bag_id)
            ledger = plan.used_attempts
            if sort_rows != ledger:
                discrepancies.append(
                    f"bag {plan.bag_id}: ledger counts {ledger} injected attempts "
                    f"but sort log holds {sort_rows} reinjection records"
                )
            for logged in plan.attempts:
                if logged.outcome == ReinjectOutcome.INJECTED.value and not (
                    self._sorter.log.has_attempt(logged.bag_id, logged.attempt_number)
                ):
                    discrepancies.append(
                        f"bag {plan.bag_id}: attempt {logged.attempt_number} "
                        "counted without a matching sort record"
                    )
            if plan.status == "rechecking" and plan.bag_id not in state.recheck_queue:
                discrepancies.append(
                    f"bag {plan.bag_id}: plan is rechecking but absent from the queue"
                )
        for handoff in state.manual_handoffs:
            bag = state.bags.get(handoff.bag_id)
            if bag is not None and bag.state is not BagState.MANUAL_HANDLING:
                discrepancies.append(
                    f"bag {handoff.bag_id}: handoff recorded but bag state is "
                    f"{bag.state.value}"
                )
        return {
            "consistent": not discrepancies,
            "discrepancies": discrepancies,
            "plans": len(state.reinject_plans),
            "handoffs": len(state.manual_handoffs),
            "injected": sum(plan.used_attempts for plan in state.reinject_plans.values()),
        }

    # -- internals ---------------------------------------------------------

    def _block_reason(
        self, plan: ReinjectPlan
    ) -> tuple[ReinjectOutcome, str, str] | None:
        """The gate that stops a due attempt from being injected, if any."""

        flight = self._book.get(plan.flight_id)
        if not is_open_state(flight.state):
            return (
                ReinjectOutcome.FLIGHT_CLOSED,
                f"flight {plan.flight_id!r} is {flight.state.value}",
                "reinject",
            )
        if policy.parse_iso(self._clock.now_iso()) > policy.latest_deadline(
            plan.cutoff_at, plan.guard_seconds
        ):
            return (
                ReinjectOutcome.PAST_DEADLINE,
                f"past the guard deadline for cutoff {plan.cutoff_at}",
                "reinject",
            )
        if self._chutes.current(plan.flight_id) is None:
            return (
                ReinjectOutcome.NO_CHUTE,
                f"flight {plan.flight_id!r} has no assigned chute",
                "chute",
            )
        if not self._quota.allowed(plan.flight_id):
            return (
                ReinjectOutcome.QUOTA_EXHAUSTED,
                "flight reinjection allowance is exhausted",
                "reinject",
            )
        return None

    def _handoff(
        self,
        bag: Bag,
        reason: str,
        blocked_stage: str,
        plan: ReinjectPlan | None = None,
        settings: policy.Policy | None = None,
        cutoff_at: str = "",
        schedule_generation: int = 0,
        blocking: tuple[str, int, str, ReinjectOutcome, str] | None = None,
    ) -> ReinjectPlan:
        existing = plan or self._store.state().reinject_plans.get(bag.id)
        chosen = settings or policy.Policy()
        at = self._clock.now_iso()
        attempts = list(existing.attempts) if existing else []
        if blocking is not None:
            key, number, slot_at, outcome, block_reason = blocking
            attempts.append(
                ReinjectAttempt(
                    key=key,
                    bag_id=bag.id,
                    attempt_number=number,
                    slot_at=slot_at,
                    outcome=outcome.value,
                    at=at,
                    reason=block_reason,
                )
            )
        handoff = ManualHandoff(
            bag_id=bag.id,
            flight_id=bag.flight_id,
            reason=reason,
            blocked_stage=blocked_stage,
            attempts_used=sum(1 for item in attempts if item.outcome == "injected"),
            max_attempts=existing.max_attempts if existing else chosen.max_attempts,
            at=at,
        )
        updated_bag = bag.evolved(state=BagState.MANUAL_HANDLING)
        if existing is not None:
            closed = existing.evolved(status="handoff", attempts=attempts)
        else:
            closed = ReinjectPlan(
                bag_id=bag.id,
                flight_id=bag.flight_id,
                max_attempts=chosen.max_attempts,
                interval_seconds=chosen.interval_seconds,
                guard_seconds=chosen.guard_seconds,
                cutoff_at=cutoff_at,
                schedule_generation=schedule_generation,
                slots=[],
                status="handoff",
                attempts=attempts,
                tradeoff=policy.TRADEOFF,
                rejected_alternative=policy.REJECTED_ALTERNATIVE,
                created_at=at,
            )
        with self._store.session() as session:
            session.state.manual_handoffs.append(handoff)
            session.state.reinject_plans[bag.id] = closed
            session.state.bags[bag.id] = updated_bag
            session.record("manual.handoff", {"handoff": handoff.as_dict()}, at)
            session.record("reinject.plan.put", {"plan": closed.as_dict()}, at)
            session.record("bag.put", {"bag": updated_bag.as_dict()}, at)
            session.record("counter.add", {"name": "reinject.handoff", "delta": 1}, at)
        return closed

    def _find_attempt(self, plan: ReinjectPlan, key: str) -> ReinjectAttempt | None:
        for attempt in plan.attempts:
            if attempt.key == key:
                return attempt
        return None

    def _bag(self, bag_id: str) -> Bag:
        bag = self._store.state().bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        return bag
