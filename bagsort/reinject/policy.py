"""Reinjection slot policy.

Two policies were considered:

* **cutoff anchored (selected)** — each attempt's slot is computed backwards
  from the flight cutoff minus a guard margin, in fixed intervals. Every slot
  is therefore provably early enough to still make the flight.
* **completion ordered (rejected)** — slots are handed out in the order bags
  finish recheck, counting forward from "now". Near the cutoff that policy
  produces different, worse outcomes: a bag that finishes recheck late is
  cheerfully assigned slots that fall after cutoff and can never fly, while the
  queue length, not the deadline, decides who gets the last viable slot.

The selected policy trades a little flexibility (bags cannot use the time
between recheck completion and the first backward-planned slot) for a hard
guarantee: *no bag is ever told to reinject at a time that would miss its
flight*. The chosen policy, the rejected alternative and that tradeoff are
copied onto every plan and into the audit trail, so the decision is on the
record rather than living in this comment alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bagsort.model.state import DEFAULT_RECHECK_CAPACITY

POLICY = "cutoff_anchored"
REJECTED_ALTERNATIVE = "completion_ordered"
TRADEOFF = (
    "slots are counted backward from cutoff minus a guard margin instead of "
    "forward from recheck completion; this forgoes any slack before the first "
    "slot but guarantees every scheduled slot can still make the flight. The "
    "completion ordered alternative assigns post-cutoff slots near deadline and "
    "lets queue position, not the deadline, decide who gets the last viable slot"
)

DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_GUARD_SECONDS = 90
DEFAULT_RECHECK_CAPACITY = 8
UNLIMITED = -1

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _FORMAT).replace(tzinfo=timezone.utc)


def format_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_FORMAT)


@dataclass(frozen=True)
class Policy:
    """The tunable knobs of the cutoff anchored policy."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    guard_seconds: int = DEFAULT_GUARD_SECONDS

    def validate(self) -> "Policy":
        if self.max_attempts < 1:
            raise ValueError("max attempts must be at least one")
        if self.interval_seconds < 1:
            raise ValueError("interval must be at least one second")
        if self.guard_seconds < 0:
            raise ValueError("guard margin must not be negative")
        return self


def latest_deadline(cutoff_at: str, guard_seconds: int) -> datetime:
    """The latest instant a reinjection may happen at and still make the flight."""

    return parse_iso(cutoff_at) - timedelta(seconds=guard_seconds)


def plan_slots(
    cutoff_at: str,
    not_before: str,
    max_attempts: int,
    interval_seconds: int,
    guard_seconds: int,
) -> list[str]:
    """Backward-planned, ascending slot instants no earlier than ``not_before``.

    The last slot sits on the guard deadline, the one before it one interval
    earlier, and so on. Slots that would fall before ``not_before`` (the bag's
    recheck completion) are dropped rather than scheduled in the past.
    """

    policy = Policy(max_attempts, interval_seconds, guard_seconds).validate()
    if not cutoff_at:
        raise ValueError("cutoff is not set; cannot anchor reinjection slots")
    floor = parse_iso(not_before)
    deadline = latest_deadline(cutoff_at, policy.guard_seconds)
    raw = [
        deadline - timedelta(seconds=policy.interval_seconds * offset)
        for offset in range(policy.max_attempts - 1, -1, -1)
    ]
    return [format_iso(instant) for instant in raw if instant >= floor]
