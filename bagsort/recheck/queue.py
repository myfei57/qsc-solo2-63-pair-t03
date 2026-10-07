"""Recheck queue: strictly first in, first out.

Bags are intercepted in the order they arrive and processed in that same order.
The queue is part of the committed document, so a restart resumes the backlog
instead of silently reordering it.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import QuotaError
from bagsort.model.entities import Bag
from bagsort.model.enums import BagState
from bagsort.model.errors import unknown_bag
from bagsort.store.store import Store


class RecheckQueue:
    """FIFO backlog of bags waiting for a second look."""

    def __init__(self, store: Store, book: object, clock: Clock) -> None:
        self._store = store
        self._book = book
        self._clock = clock

    def enqueue(self, bag: Bag) -> int:
        updated = bag.evolved(state=BagState.RECHECKED)
        at = self._clock.now_iso()
        with self._store.session() as session:
            already = updated.id in session.state.recheck_queue
            capacity = session.state.recheck_capacity
            if not already and capacity and len(session.state.recheck_queue) >= capacity:
                # Explicit backpressure: the caller must hand the bag off
                # instead of the queue silently growing past station capacity.
                raise QuotaError(
                    f"recheck station backlog is at capacity {capacity}"
                )
            session.state.bags[updated.id] = updated
            if not already:
                session.state.recheck_queue.append(updated.id)
            session.record("bag.put", {"bag": updated.as_dict()}, at)
            session.record("recheck.enqueue", {"bag_id": updated.id}, at)
            session.record("counter.add", {"name": "recheck.enqueued", "delta": 1}, at)
        return len(self._store.state().recheck_queue)

    def dequeue(self) -> Bag | None:
        state = self._store.state()
        if not state.recheck_queue:
            return None
        bag_id = state.recheck_queue[0]
        bag = state.bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.recheck_queue = [
                item for item in session.state.recheck_queue if item != bag_id
            ]
            session.record("recheck.dequeue", {"bag_id": bag_id}, at)
            session.record("counter.add", {"name": "recheck.processed", "delta": 1}, at)
        return bag

    def peek(self) -> Bag | None:
        state = self._store.state()
        if not state.recheck_queue:
            return None
        return state.bags.get(state.recheck_queue[0])

    def items(self) -> list[Bag]:
        state = self._store.state()
        return [state.bags[bag_id] for bag_id in state.recheck_queue if bag_id in state.bags]

    def length(self) -> int:
        return len(self._store.state().recheck_queue)

    def clear(self) -> int:
        dropped = len(self._store.state().recheck_queue)
        with self._store.session() as session:
            session.state.recheck_queue = []
            session.record("recheck.clear", {}, self._clock.now_iso())
        return dropped

    def deadline(self, flight_id: str) -> str:
        return self._book.deadline(flight_id)
