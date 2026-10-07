"""Enumerations for the two state machines the plant tracks."""

from __future__ import annotations

from enum import Enum


class FlightState(str, Enum):
    """Lifecycle of one flight batch."""

    OPEN = "open"
    BOARDING = "boarding"
    CUTOFF = "cutoff"
    CLOSED = "closed"


class BagState(str, Enum):
    """Lifecycle of one piece of baggage."""

    CHECKED_IN = "checked_in"
    INJECTED = "injected"
    SORTED = "sorted"
    RECHECKED = "rechecked"
    REINJECTING = "reinjecting"
    MANUAL_HANDLING = "manual_handling"
    LOADED = "loaded"
    DIVERTED = "diverted"

    @property
    def active(self) -> bool:
        """Whether the bag still occupies plant capacity."""

        return self in (
            BagState.CHECKED_IN,
            BagState.INJECTED,
            BagState.SORTED,
            BagState.RECHECKED,
            BagState.REINJECTING,
        )


class ReinjectOutcome(str, Enum):
    """What happened when one scheduled reinjection attempt was triggered."""

    INJECTED = "injected"
    DUPLICATE = "duplicate"
    PENDING_SLOT = "pending_slot"
    QUOTA_EXHAUSTED = "quota_exhausted"
    PAST_DEADLINE = "past_deadline"
    FLIGHT_CLOSED = "flight_closed"
    NO_CHUTE = "no_chute"
    HANDOFF = "handoff"

class Verdict(str, Enum):
    """Result of comparing an observed (epoch, sequence) pair with the high-water mark."""

    FRESH = "fresh"
    REPLAYED = "replayed"
    ROLLED_OVER = "rolled_over"
