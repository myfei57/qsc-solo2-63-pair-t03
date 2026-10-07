"""Per-flight reinjection allowance.

Separate from the check-in sorting allowance: the sorting allowance limits how
many bags a flight accepts up front, while this ledger limits how many
*reinjections* (re-drives after a failed recheck) the flight may consume.
Consumption is committed in the same session as the sort record, so the
remaining balance always agrees with the sort ledger's ``attempt >= 1`` rows.
A flight with no entry is unlimited.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ValidationError
from bagsort.store.store import Store

UNLIMITED = -1


class ReinjectQuota:
    """Tracks how many more reinjections each flight may take."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def set_limit(self, flight_id: str, limit: int) -> int:
        if not flight_id:
            raise ValidationError("flight id must not be empty")
        if limit < -1:
            raise ValidationError("reinject limit must not be below -1 (unlimited)")
        with self._store.session() as session:
            session.state.reinject_limits[flight_id] = limit
            session.record(
                "reinject.limit.set",
                {"flight_id": flight_id, "limit": limit},
                self._clock.now_iso(),
            )
        return limit

    def limit(self, flight_id: str) -> int:
        return self._store.state().reinject_limits.get(flight_id, UNLIMITED)

    def available(self, flight_id: str) -> int:
        return self.limit(flight_id)

    def allowed(self, flight_id: str) -> bool:
        remaining = self.limit(flight_id)
        return remaining < 0 or remaining > 0

    def consume(self, flight_id: str, count: int = 1) -> bool:
        """Reserve ``count`` reinjections; refuses instead of going negative."""

        if count < 0:
            raise ValidationError("consume count must not be negative")
        remaining = self.limit(flight_id)
        if remaining < 0:
            return True
        if remaining - count < 0:
            return False
        with self._store.session() as session:
            self.apply_consume(session, flight_id, count)
        return True

    def apply_consume(self, session, flight_id: str, count: int = 1) -> bool:
        """Consume inside a caller-owned session so the ledger commit is atomic.

        Returns ``False`` (without writing) when the balance cannot cover the
        reservation; the caller's session then has no quota command in it.
        """

        remaining = session.state.reinject_limits.get(flight_id, UNLIMITED)
        if remaining < 0:
            return True
        if remaining - count < 0:
            return False
        session.state.reinject_limits[flight_id] = remaining - count
        session.record(
            "reinject.limit.adjust",
            {"flight_id": flight_id, "delta": -count},
            self._clock.now_iso(),
        )
        return True

    def release(self, flight_id: str, count: int = 1) -> int:
        if count < 0:
            raise ValidationError("release count must not be negative")
        remaining = self.limit(flight_id)
        if remaining < 0:
            return UNLIMITED
        with self._store.session() as session:
            session.state.reinject_limits[flight_id] = remaining + count
            session.record(
                "reinject.limit.adjust",
                {"flight_id": flight_id, "delta": count},
                self._clock.now_iso(),
            )
        return remaining + count

    def limits(self) -> dict[str, int]:
        return dict(sorted(self._store.state().reinject_limits.items()))

    def status(self) -> list[dict[str, object]]:
        return [
            {
                "flight_id": flight_id,
                "limit": limit,
                "unlimited": limit < 0,
                "allowed": limit < 0 or limit > 0,
            }
            for flight_id, limit in sorted(self._store.state().reinject_limits.items())
        ]
