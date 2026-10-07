"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import BagState, FlightState


def _clean(raw: Any) -> Any:
    """Normalise nested containers so codecs round-trip deterministically."""

    if isinstance(raw, dict):
        return {str(key): _clean(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_clean(item) for item in raw]
    return raw


@dataclass(frozen=True)
class Flight:
    id: str
    code: str
    terminal: str = "T1"
    carousel: str = ""
    chute: str = ""
    state: FlightState = FlightState.OPEN
    batch: int = 0
    schedule_generation: int = 1
    cutoff_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "terminal": self.terminal,
            "carousel": self.carousel,
            "chute": self.chute,
            "state": self.state.value,
            "batch": self.batch,
            "schedule_generation": self.schedule_generation,
            "cutoff_at": self.cutoff_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Flight":
        return cls(
            id=str(raw["id"]),
            code=str(raw.get("code", "")),
            terminal=str(raw.get("terminal", "T1")),
            carousel=str(raw.get("carousel", "")),
            chute=str(raw.get("chute", "")),
            state=FlightState(raw.get("state", FlightState.OPEN.value)),
            batch=int(raw.get("batch", 0)),
            schedule_generation=int(raw.get("schedule_generation", 1)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            closed_at=str(raw.get("closed_at", "")),
        )

    def evolved(self, **changes: Any) -> "Flight":
        return replace(self, **changes)


@dataclass(frozen=True)
class Bag:
    id: str
    barcode: str
    sequence: int
    epoch: int = 0
    flight_id: str = ""
    chute_id: str = ""
    state: BagState = BagState.CHECKED_IN

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "barcode": self.barcode,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Bag":
        return cls(
            id=str(raw["id"]),
            barcode=str(raw.get("barcode", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            state=BagState(raw.get("state", BagState.CHECKED_IN.value)),
        )

    def evolved(self, **changes: Any) -> "Bag":
        return replace(self, **changes)


@dataclass(frozen=True)
class Reading:
    barcode: str
    bag_id: str
    sequence: int
    epoch: int
    committed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "bag_id": self.bag_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reading":
        return cls(
            barcode=str(raw.get("barcode", "")),
            bag_id=str(raw.get("bag_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            committed=bool(raw.get("committed", False)),
        )


@dataclass(frozen=True)
class ChuteAssignment:
    flight_id: str
    chute_id: str
    generation: int = 1
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChuteAssignment":
        return cls(
            flight_id=str(raw["flight_id"]),
            chute_id=str(raw.get("chute_id", "")),
            generation=int(raw.get("generation", 1)),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass(frozen=True)
class SortRecord:
    bag_id: str
    flight_id: str
    chute_id: str
    sequence: int
    epoch: int
    at: str
    attempt: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "at": self.at,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SortRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            at=str(raw.get("at", "")),
            attempt=int(raw.get("attempt", 0)),
        )


@dataclass(frozen=True)
class TransferCommand:
    id: str
    flight_id: str
    carousel: str
    batch: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flight_id": self.flight_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TransferCommand":
        return cls(
            id=str(raw["id"]),
            flight_id=str(raw.get("flight_id", "")),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class LoadRecord:
    bag_id: str
    carousel: str
    batch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class Sequence:
    epoch: int = 0
    next_value: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "next": self.next_value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Sequence":
        return cls(epoch=int(raw.get("epoch", 0)), next_value=int(raw.get("next", 0)))


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)
    prev_digest: str = ""
    digest: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "subject": self.subject,
            "detail": _clean(self.detail),
            "prev_digest": self.prev_digest,
            "digest": self.digest,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            kind=str(raw.get("kind", "")),
            subject=str(raw.get("subject", "")),
            detail=_clean(raw.get("detail", {})),
            prev_digest=str(raw.get("prev_digest", "")),
            digest=str(raw.get("digest", "")),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class ReinjectPlan:
    """The system decision for one bag that failed recheck.

    ``attempts`` is the ordered list of scheduled slot instants. The plan is
    anchored to the flight cutoff and the schedule generation it was computed
    against, so a schedule revision visibly invalidates it instead of silently
    moving the slots.
    """

    bag_id: str
    flight_id: str
    max_attempts: int
    interval_seconds: int
    guard_seconds: int
    cutoff_at: str
    schedule_generation: int
    slots: list[str]
    attempts: list["ReinjectAttempt"] = field(default_factory=list)
    status: str = "scheduled"
    policy: str = "cutoff_anchored"
    rejected_alternative: str = "completion_ordered"
    tradeoff: str = ""
    created_at: str = ""

    @property
    def used_attempts(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.outcome == "injected")

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "max_attempts": self.max_attempts,
            "interval_seconds": self.interval_seconds,
            "guard_seconds": self.guard_seconds,
            "cutoff_at": self.cutoff_at,
            "schedule_generation": self.schedule_generation,
            "slots": list(self.slots),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "status": self.status,
            "policy": self.policy,
            "rejected_alternative": self.rejected_alternative,
            "tradeoff": self.tradeoff,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReinjectPlan":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            max_attempts=int(raw.get("max_attempts", 0)),
            interval_seconds=int(raw.get("interval_seconds", 0)),
            guard_seconds=int(raw.get("guard_seconds", 0)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            schedule_generation=int(raw.get("schedule_generation", 1)),
            slots=[str(item) for item in raw.get("slots", [])],
            attempts=[
                ReinjectAttempt.from_dict(item) for item in raw.get("attempts", [])
            ],
            status=str(raw.get("status", "scheduled")),
            policy=str(raw.get("policy", "cutoff_anchored")),
            rejected_alternative=str(raw.get("rejected_alternative", "")),
            tradeoff=str(raw.get("tradeoff", "")),
            created_at=str(raw.get("created_at", "")),
        )

    def evolved(self, **changes: Any) -> "ReinjectPlan":
        return replace(self, **changes)


@dataclass(frozen=True)
class ReinjectAttempt:
    """One committed reinjection attempt, keyed for idempotency."""

    key: str
    bag_id: str
    attempt_number: int
    slot_at: str
    outcome: str
    at: str
    sort_record_at: str = ""
    chute_id: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "bag_id": self.bag_id,
            "attempt_number": self.attempt_number,
            "slot_at": self.slot_at,
            "outcome": self.outcome,
            "at": self.at,
            "sort_record_at": self.sort_record_at,
            "chute_id": self.chute_id,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReinjectAttempt":
        return cls(
            key=str(raw["key"]),
            bag_id=str(raw.get("bag_id", "")),
            attempt_number=int(raw.get("attempt_number", 0)),
            slot_at=str(raw.get("slot_at", "")),
            outcome=str(raw.get("outcome", "")),
            at=str(raw.get("at", "")),
            sort_record_at=str(raw.get("sort_record_at", "")),
            chute_id=str(raw.get("chute_id", "")),
            reason=str(raw.get("reason", "")),
        )


@dataclass(frozen=True)
class ManualHandoff:
    """A bag handed to manual handling, with the step it got stuck at."""

    bag_id: str
    flight_id: str
    reason: str
    blocked_stage: str
    attempts_used: int
    max_attempts: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "reason": self.reason,
            "blocked_stage": self.blocked_stage,
            "attempts_used": self.attempts_used,
            "max_attempts": self.max_attempts,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ManualHandoff":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            reason=str(raw.get("reason", "")),
            blocked_stage=str(raw.get("blocked_stage", "")),
            attempts_used=int(raw.get("attempts_used", 0)),
            max_attempts=int(raw.get("max_attempts", 0)),
            at=str(raw.get("at", "")),
        )
