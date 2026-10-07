"""Assembly of the handling line.

Every subsystem is constructed here and every operator visible action is a
method on :class:`Plant`, which is what keeps the console handlers thin and
lets the CLI drive the same operations the HTTP API exposes.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.audit.chain import AuditChain
from bagsort.belt.belt import Belt
from bagsort.checkin.desk import Desk
from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.events.bus import EventBus
from bagsort.flight.book import FlightBook
from bagsort.flight.close import FlightCloser
from bagsort.flight.mapping import BagMapping
from bagsort.flight.schedule import ScheduleBoard
from bagsort.ids import IdFactory
from bagsort.inject.injector import Injector
from bagsort.model.entities import Bag, Flight, Reading, SortRecord
from bagsort.model.errors import unknown_bag
from bagsort.ns.chutes import ChuteRegistry
from bagsort.ns.namespace import Namespace
from bagsort.ns.topology import handling_line
from bagsort.quota.quota import QuotaBook
from bagsort.recheck.queue import RecheckQueue
from bagsort.reinject import policy
from bagsort.reinject.director import ReinjectDirector
from bagsort.reinject.quota import ReinjectQuota
from bagsort.reporting.report import PlantReport
from bagsort.sequence.verdict import VerdictTracker
from bagsort.sorter.sorter import Sorter
from bagsort.store.store import Store
from bagsort.tag.history import ReadHistory
from bagsort.tag.reader import TagReader
from bagsort.chute.assigner import ChuteAssigner
from bagsort.chute.history import AssignmentHistory
from bagsort.console.metrics import Metrics
from bagsort.console.telemetry import Telemetry

DEFAULT_TERMINAL = "T1"


@dataclass
class Plant:
    """The wired plant plus the operator level operations the console calls."""

    store: Store
    clock: Clock
    namespace: Namespace
    registry: ChuteRegistry
    bus: EventBus
    telemetry: Telemetry
    metrics: Metrics
    reader: TagReader
    reads: ReadHistory
    book: FlightBook
    mapping: BagMapping
    schedule: ScheduleBoard
    closer: FlightCloser
    chutes: ChuteAssigner
    chute_history: AssignmentHistory
    sorter: Sorter
    injector: Injector
    desk: Desk
    recheck: RecheckQueue
    reinject: ReinjectDirector
    reinject_quota: ReinjectQuota
    belt: Belt
    quota: QuotaBook
    audit: AuditChain
    report: PlantReport

    @property
    def topology(self):
        return handling_line()

    # -- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        store: Store,
        terminal: str = DEFAULT_TERMINAL,
        clock: Clock | None = None,
    ) -> "Plant":
        clock = clock or Clock()
        namespace = Namespace(terminal)
        registry = ChuteRegistry()
        bus = EventBus()
        telemetry = Telemetry()
        metrics = Metrics()
        bus.subscribe("*", lambda event: telemetry.observe(event.topic))

        reader = TagReader(store, namespace, clock)
        reads = ReadHistory(store)
        book = FlightBook(store, clock)
        mapping = BagMapping(store, clock)
        schedule = ScheduleBoard(store, clock)
        chutes = ChuteAssigner(store, clock)
        chute_history = AssignmentHistory(store)
        sorter = Sorter(store, mapping, chutes, clock, VerdictTracker())
        injector = Injector(store, book, sorter, clock)
        quota = QuotaBook(store, clock)
        reinject_quota = ReinjectQuota(store, clock)
        desk = Desk(store, reader, book, mapping, injector, quota, clock, handling_line())
        closer = FlightCloser(book)
        recheck = RecheckQueue(store, book, clock)
        reinject = ReinjectDirector(store, book, chutes, sorter, reinject_quota, clock)
        belt = Belt(store, clock, IdFactory("cmd"))

        audit = AuditChain(store, clock, handling_line())
        audit_subscriber = _AuditSubscriber(audit)
        for topic in (
            "bag.checked_in",
            "bag.sorted",
            "bag.diverted",
            "bag.recheck_enqueued",
            "bag.reinjection_planned",
            "bag.reinjected",
            "bag.reinjection_blocked",
            "bag.manual_handoff",
            "flight.closed",
            "chute.assigned",
            "belt.switched",
            "bag.remapped",
        ):
            bus.subscribe(topic, audit_subscriber)

        report = PlantReport(
            store,
            book,
            chutes,
            belt,
            quota,
            audit,
            namespace,
            handling_line(),
            reinject=reinject,
            reinject_quota=reinject_quota,
        )
        return cls(
            store=store,
            clock=clock,
            namespace=namespace,
            registry=registry,
            bus=bus,
            telemetry=telemetry,
            metrics=metrics,
            reader=reader,
            reads=reads,
            book=book,
            mapping=mapping,
            schedule=schedule,
            closer=closer,
            chutes=chutes,
            chute_history=chute_history,
            sorter=sorter,
            injector=injector,
            desk=desk,
            recheck=recheck,
            reinject=reinject,
            reinject_quota=reinject_quota,
            belt=belt,
            quota=quota,
            audit=audit,
            report=report,
        )

    # -- operator operations ----------------------------------------------

    def register_flight(self, code: str, carousel: str = "", chute: str = "") -> Flight:
        flight = Flight(
            id=self.namespace.flight_id(code),
            code=code.strip().upper(),
            terminal=self.namespace.terminal,
            carousel=self.namespace.carousel_id(carousel) if carousel else "",
        )
        self.book.register(flight)
        if chute:
            self.assign_chute(flight.id, self.namespace.chute_id(chute))
        return self.book.get(flight.id)

    def assign_chute(self, flight_id: str, chute_id: str):
        assignment = self.chutes.assign(flight_id, chute_id)
        self.bus.publish("chute.assigned", flight_id=flight_id, chute_id=chute_id)
        return assignment

    def read_barcode(self, barcode: str) -> Reading:
        return self.reader.read(barcode)

    def check_in(self, barcode: str, flight_id: str) -> Bag:
        bag = self.desk.check_in(barcode, flight_id)
        self.bus.publish("bag.checked_in", bag_id=bag.id, flight_id=flight_id)
        return bag

    def cancel_check_in(self, bag_id: str) -> Bag:
        bag = self.desk.cancel(bag_id)
        self.bus.publish("bag.diverted", bag_id=bag.id, reason="checkin_cancelled")
        return bag

    def bag(self, bag_id: str) -> Bag:
        bag = self.store.state().bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        return bag

    def push_bag(self, bag_id: str, chute_id: str) -> SortRecord:
        bag = self.bag(bag_id)
        record = self.injector.push(bag, chute_id)
        self.bus.publish("bag.sorted", bag_id=bag.id, chute_id=record.chute_id)
        return record

    def retry_bag(self, bag_id: str) -> bool:
        bag = self.bag(bag_id)
        sorted_now = self.injector.retry(bag)
        if sorted_now:
            self.bus.publish("bag.sorted", bag_id=bag.id, retry=True)
        return sorted_now

    def divert_bag(self, bag_id: str) -> Bag:
        bag = self.bag(bag_id)
        updated = self.sorter.divert(bag)
        self.bus.publish("bag.diverted", bag_id=bag.id)
        return updated

    def remap_bag(self, bag_id: str, flight_id: str) -> Bag:
        self.book.require_open(flight_id)
        self.mapping.remap(bag_id, flight_id)
        self.bus.publish("bag.remapped", bag_id=bag_id, flight_id=flight_id)
        return self.bag(bag_id)

    def revise_schedule(self, flight_id: str, cutoff_at: str):
        return self.schedule.revise(flight_id, cutoff_at)

    def close_flight(self, flight_id: str) -> Flight:
        flight = self.closer.close(flight_id, self.injector)
        self.bus.publish("flight.closed", flight_id=flight_id)
        return flight

    def enqueue_recheck(self, bag_id: str) -> Bag:
        bag = self.bag(bag_id)
        self.recheck.enqueue(bag)
        self.bus.publish("bag.recheck_enqueued", bag_id=bag_id)
        return bag

    def process_recheck(self) -> Bag | None:
        return self.recheck.dequeue()

    def set_recheck_capacity(self, capacity: int) -> int:
        return self.reinject.set_recheck_capacity(capacity)

    def fail_recheck(
        self, bag_id: str, settings: policy.Policy | None = None
    ):
        """Station verdict after dequeue: a failed recheck becomes a plan.

        Enters through the backpressure-limited admit path, so an
        over-capacity station refuses before any plan is committed.
        """

        plan = self.reinject.admit(self.bag(bag_id), settings)
        self._publish_planned(plan)
        return plan

    def _publish_planned(self, plan) -> None:
        self.bus.publish(
            "bag.reinjection_planned",
            bag_id=plan.bag_id,
            flight_id=plan.flight_id,
            policy=plan.policy,
            rejected_alternative=plan.rejected_alternative,
            max_attempts=plan.max_attempts,
            slots=list(plan.slots),
            tradeoff=plan.tradeoff,
        )

    def trigger_reinjection(self, bag_id: str):
        """Idempotently fire the next attempt; returns the trigger result."""

        result = self.reinject.trigger(bag_id)
        if result.outcome == "injected":
            self.bus.publish(
                "bag.reinjected",
                bag_id=bag_id,
                attempt=result.attempt_number,
            )
        elif result.outcome == "handoff":
            handoff = self.reinject.handoff_for(bag_id)
            reason = handoff.reason if handoff else result.outcome
            stage = handoff.blocked_stage if handoff else "reinject"
            self.bus.publish(
                "bag.reinjection_blocked",
                bag_id=bag_id,
                blocked_stage=stage,
                outcome=result.outcome,
                reason=reason,
            )
            self.bus.publish(
                "bag.manual_handoff",
                bag_id=bag_id,
                blocked_stage=stage,
                reason=reason,
            )
        return result

    def trigger_due_reinjections(self) -> list:
        """The bounded station sweep: fire every due attempt at most once."""

        results = self.reinject.trigger_due()
        for result in results:
            if result.outcome == "injected":
                self.bus.publish(
                    "bag.reinjected",
                    bag_id=result.bag_id,
                    attempt=result.attempt_number,
                )
            elif result.outcome == "handoff":
                handoff = self.reinject.handoff_for(result.bag_id)
                reason = handoff.reason if handoff else result.outcome
                stage = handoff.blocked_stage if handoff else "reinject"
                self.bus.publish(
                    "bag.reinjection_blocked",
                    bag_id=result.bag_id,
                    blocked_stage=stage,
                    outcome=result.outcome,
                    reason=reason,
                )
                self.bus.publish(
                    "bag.manual_handoff",
                    bag_id=result.bag_id,
                    blocked_stage=stage,
                    reason=reason,
                )
        return results

    def report_recheck_failure(self, bag_id: str, reason: str = "recheck failed again"):
        """The reinjected bag failed recheck again: reschedule or hand off."""

        plan = self.reinject.fail_current(bag_id, reason)
        if plan.status == "handoff":
            handoff = self.reinject.handoff_for(bag_id)
            self.bus.publish(
                "bag.manual_handoff",
                bag_id=bag_id,
                blocked_stage=handoff.blocked_stage if handoff else "recheck",
                reason=handoff.reason if handoff else reason,
            )
        return plan

    def handoff_bag(self, bag_id: str, reason: str, blocked_stage: str = "reinject"):
        """Send a stuck bag to manual handling explicitly (never drop it)."""

        handoff = self.reinject.handoff(bag_id, reason, blocked_stage)
        self.bus.publish(
            "bag.manual_handoff",
            bag_id=bag_id,
            blocked_stage=blocked_stage,
            reason=reason,
        )
        return handoff

    def reinjection_reconcile(self) -> dict[str, object]:
        return self.reinject.reconcile()

    def switch_belt(self, carousel: str, batch: int) -> int:
        cleared = self.belt.switch(carousel, batch)
        self.bus.publish("belt.switched", carousel=carousel, batch=batch)
        return cleared

    def rotate_belt(self) -> int:
        return self.belt.rotate()

    def append_transfer(self, flight_id: str, carousel: str, batch: int):
        return self.belt.append_transfer(flight_id, carousel, batch)

    def set_quota(self, flight_id: str, limit: int) -> int:
        return self.quota.set_limit(flight_id, limit)

    def verify(self) -> bool:
        return self.audit.verify()


class _AuditSubscriber:
    """Turns published operations into linked audit entries."""

    _STAGE = {
        "bag.checked_in": ("checkin", "checkin"),
        "bag.sorted": ("sort", "sorter"),
        "bag.diverted": ("diversion", "sorter"),
        "bag.recheck_enqueued": ("recheck", "recheck"),
        "bag.reinjection_planned": ("reinject_plan", "recheck"),
        "bag.reinjected": ("reinjection", "reinject"),
        "bag.reinjection_blocked": ("reinjection_block", "reinject"),
        "bag.manual_handoff": ("manual_handoff", "reinject"),
        "flight.closed": ("flight_close", "audit"),
        "chute.assigned": ("chute_assign", "chute"),
        "belt.switched": ("belt_switch", "belt"),
        "bag.remapped": ("remap", "checkin"),
    }

    def __init__(self, audit: AuditChain) -> None:
        self._audit = audit

    def __call__(self, event) -> None:
        mapping = self._STAGE.get(event.topic)
        if mapping is None:
            raise ConflictError(f"no audit mapping for topic {event.topic!r}")
        kind, stage = mapping
        subject = str(
            event.payload.get("bag_id")
            or event.payload.get("flight_id")
            or event.payload.get("carousel")
            or ""
        )
        detail = {
            key: value
            for key, value in event.payload.items()
            if key not in {"bag_id", "flight_id"}
        }
        self._audit.record(kind, subject, stage, detail)
