"""Dispatch: the step that turns a committed reading into a sort record.

Dispatch refuses an uncommitted reading outright, and it refuses a bag that has
already been sorted. Both refusals are the contracts other subsystems rely on:
the desk must commit before routing, and a retried induction must not create a
second sort record.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.model.entities import Bag, Reading, SortRecord
from bagsort.model.enums import BagState
from bagsort.sequence.verdict import VerdictTracker
from bagsort.store.store import Store
from bagsort.chute.assigner import ChuteAssigner
from bagsort.flight.mapping import BagMapping
from .records import SortLog
from .route import RouteDetail


class Sorter:
    """Routes committed bags to their flight's current chute."""

    def __init__(
        self,
        store: Store,
        mapping: BagMapping,
        chutes: ChuteAssigner,
        clock: Clock,
        verdict: VerdictTracker | None = None,
    ) -> None:
        self._store = store
        self._mapping = mapping
        self._chutes = chutes
        self._clock = clock
        self._verdict = verdict or VerdictTracker()
        self._log = SortLog(store)

    @property
    def log(self) -> SortLog:
        return self._log

    def route(self, bag_id: str) -> RouteDetail:
        flight_id = self._mapping.flight_of(bag_id)
        assignment = self._chutes.require(flight_id)
        return RouteDetail(
            bag_id=bag_id,
            flight_id=flight_id,
            chute_id=assignment.chute_id,
            generation=assignment.generation,
        )

    def dispatch(self, bag: Bag, reading: Reading) -> SortRecord:
        """First-time dispatch (attempt 0); refuses if the bag was ever sorted."""

        if self._log.was_executed(bag.id):
            raise ConflictError(f"bag {bag.id!r} has already been sorted")
        if not reading.committed:
            raise ConflictError(f"barcode read for {bag.barcode!r} is not committed")
        with self._store.session() as session:
            return self.apply_attempt(session, bag, reading, attempt=0)

    def dispatch_attempt(self, bag: Bag, reading: Reading, attempt: int) -> SortRecord:
        """Re-drive a bag after recheck under a positive attempt number.

        Idempotent per ``(bag, attempt)``: a repeated trigger for the same
        attempt raises rather than writing a second sort record. Different
        attempt numbers are distinct records, which is what the reinjection
        ledger cross-checks against.
        """

        self._validate_attempt(bag, attempt)
        if attempt < 1:
            raise ConflictError("a reinjection attempt must be numbered 1 or higher")
        with self._store.session() as session:
            return self.apply_attempt(session, bag, reading, attempt)

    def apply_attempt(self, session, bag: Bag, reading: Reading, attempt: int) -> SortRecord:
        """Write the sort record inside a caller-owned session.

        The reinjection director calls this in the same session that debits the
        allowance ledger, so the two either commit together or roll back
        together.
        """

        self._validate_attempt(bag, attempt)
        if not reading.committed:
            raise ConflictError(f"barcode read for {bag.barcode!r} is not committed")
        detail = self.route(bag.id)
        record = SortRecord(
            bag_id=bag.id,
            flight_id=detail.flight_id,
            chute_id=detail.chute_id,
            sequence=bag.sequence,
            epoch=bag.epoch,
            at=self._clock.now_iso(),
            attempt=attempt,
        )
        updated = bag.evolved(chute_id=detail.chute_id, state=BagState.SORTED)
        session.state.sort_records.append(record)
        session.state.bags[updated.id] = updated
        session.record("sort.record", {"record": record.as_dict()}, self._clock.now_iso())
        session.record("bag.put", {"bag": updated.as_dict()}, self._clock.now_iso())
        name = "sorter.reinjected" if attempt >= 1 else "sorter.dispatched"
        session.record(
            "counter.add", {"name": name, "delta": 1}, self._clock.now_iso()
        )
        return record

    def _validate_attempt(self, bag: Bag, attempt: int) -> None:
        if attempt < 0:
            raise ConflictError("an attempt number must not be negative")
        if attempt >= 1 and self._log.has_attempt(bag.id, attempt):
            raise ConflictError(
                f"bag {bag.id!r} reinjection attempt {attempt} was already executed"
            )

    def divert(self, bag: Bag) -> Bag:
        updated = bag.evolved(state=BagState.DIVERTED)
        with self._store.session() as session:
            session.state.bags[updated.id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, self._clock.now_iso())
            session.record(
                "counter.add", {"name": "sorter.diverted", "delta": 1}, self._clock.now_iso()
            )
        return updated

    def verdict(self, epoch: int, sequence: int):
        """Classify an observed pair and advance the mark when it is fresh."""

        return self._verdict.observe(epoch, sequence)

    def is_fresh(self, epoch: int, sequence: int) -> bool:
        return self._verdict.is_fresh(epoch, sequence)

    def high_water(self) -> tuple[int, int]:
        return self._verdict.high_water()

    def was_executed(self, bag_id: str) -> bool:
        return self._log.was_executed(bag_id)

    def records(self) -> list[SortRecord]:
        return self._log.records()

    def assign_chute(self, flight_id: str, chute_id: str):
        """Convenience for the injection belt, which sets the lane then dispatches."""

        return self._chutes.assign(flight_id, chute_id)
