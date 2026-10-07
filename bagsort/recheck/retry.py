"""Resubmission control for bags that fail recheck.

The controller, not the floor, decides how often a failed bag is resubmitted
and in which slot. Every decision is a committed journal command, so the plan
survives a restart and lands in the audit trail.

Slot strategy — **cutoff anchored**, chosen over completion-order spacing:

* A resubmission is only worth anything if the bag can still make its flight,
  so the slot must satisfy ``slot <= cutoff - min_cycle``. Near the cutoff
  this escalates the bag to manual handling instead of letting it wait out a
  spacing interval it would not survive.
* The rejected alternative, plain completion-order spacing, keeps bags waiting
  for their interval even when the remaining window is too small. Near the
  cutoff it would spend attempts and line capacity on resubmissions that can
  never make the flight, and the bag would silently miss instead of being
  escalated.
* The price of anchoring: slots drift when the schedule is revised, and close
  to the cutoff several bags can be compressed into a short window. The first
  is handled by re-validating the window at execution time (a lapsed slot
  escalates instead of firing); the second by the backlog throttle, which
  refuses new schedules outright once the station is buried.

The trade-off is persisted twice: in the committed ``retry.policy`` command
(:class:`RetryPolicy` carries ``rationale``/``rejected``) and in every
``retry.schedule`` command, which records the inputs the slot was computed
from (strategy, cutoff, backlog).

Idempotency is layered: the controller refuses to schedule a bag that already
has a pending plan, and the reducer only applies ``retry.execute`` from the
scheduled state with the attempt advanced by exactly one, so a duplicated
trigger — operator, event replay, or repeated command — cannot count or fire
the same attempt twice.

Escalation never drops a bag: it is diverted to manual handling with the stage
it got stuck at and the reason recorded. Resubmissions never consume flight
allowance again — the bag already holds its slot from check-in — which is what
makes the retry ledger cross-checkable against the quota book.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bagsort.clock import Clock
from bagsort.errors import ConflictError, QuotaError, ValidationError
from bagsort.flight.book import FlightBook
from bagsort.flight.mapping import BagMapping
from bagsort.model.entities import Bag, RetryAccount, RetryPolicy
from bagsort.model.enums import BagState, RetryState
from bagsort.model.errors import unknown_bag
from bagsort.model.validate import is_open_state
from bagsort.recheck.queue import RecheckQueue
from bagsort.store.store import Store

ISO = "%Y-%m-%dT%H:%M:%SZ"

ATTEMPTS_EXHAUSTED = "attempts_exhausted"
NO_FEASIBLE_SLOT = "no_feasible_slot"
SLOT_LAPSED = "slot_lapsed"
FLIGHT_CLOSED = "flight_closed"

STUCK_STAGE = "recheck"

DEFAULT_POLICY = RetryPolicy(
    rationale=(
        "Slots are anchored to the flight cutoff: a resubmission is only "
        "planned when the bag can be back on the line no later than cutoff "
        "minus the minimum line cycle. Near the cutoff this escalates the bag "
        "to manual handling instead of letting it wait out a spacing interval "
        "and silently miss the flight."
    ),
    rejected=(
        "completion_order (fixed spacing from the recheck verdict, first come "
        "first served) was rejected: near the cutoff it keeps bags waiting for "
        "their interval even when the remaining window is too small, spending "
        "attempts and line capacity on resubmissions that can never make the "
        "flight, and the miss surfaces nowhere."
    ),
)


@dataclass(frozen=True)
class RetryOutcome:
    """The result of one resubmission decision.

    ``outcome`` tells the caller whether anything was committed, so repeated
    triggers do not produce duplicate events or audit entries.
    """

    account: RetryAccount
    outcome: str


def _parse_cutoff(raw: str) -> datetime:
    try:
        return datetime.strptime(raw, ISO).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValidationError(f"cutoff {raw!r} is not a valid timestamp") from exc


def _format(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime(ISO)


class RetryController:
    """Decides how often and when a failed bag is resubmitted."""

    def __init__(
        self,
        store: Store,
        recheck: RecheckQueue,
        book: FlightBook,
        mapping: BagMapping,
        clock: Clock,
    ) -> None:
        self._store = store
        self._recheck = recheck
        self._book = book
        self._mapping = mapping
        self._clock = clock

    # -- policy --------------------------------------------------------------

    def register_policy(self, policy: RetryPolicy) -> RetryPolicy:
        """Commit the standing rules; later registrations replace earlier ones."""

        with self._store.session() as session:
            session.state.retry_policy = policy
            session.record(
                "retry.policy", {"policy": policy.as_dict()}, self._clock.now_iso()
            )
        return policy

    def policy(self) -> RetryPolicy:
        committed = self._store.state().retry_policy
        return committed if committed is not None else DEFAULT_POLICY

    def _ensure_policy(self) -> RetryPolicy:
        committed = self._store.state().retry_policy
        if committed is not None:
            return committed
        return self.register_policy(
            DEFAULT_POLICY.evolved(registered_at=self._clock.now_iso())
        )

    # -- read side -----------------------------------------------------------

    def account(self, bag_id: str) -> RetryAccount | None:
        return self._store.state().retry_accounts.get(bag_id)

    def accounts(self) -> list[RetryAccount]:
        accounts = self._store.state().retry_accounts
        return [accounts[key] for key in sorted(accounts)]

    def due(self) -> list[RetryAccount]:
        """Scheduled accounts whose slot has arrived, oldest slot first."""

        now = self._clock.now_iso()
        selected = [
            account
            for account in self.accounts()
            if account.state is RetryState.SCHEDULED and account.slot <= now
        ]
        return sorted(selected, key=lambda account: (account.slot, account.bag_id))

    # -- decisions -----------------------------------------------------------

    def schedule(self, bag_id: str) -> RetryOutcome:
        """Decide the next resubmission for a bag that failed recheck.

        Repeated triggers for the same bag return the pending plan unchanged.
        A bag that used up its attempts, or for which no slot fits before the
        cutoff, is escalated to manual handling instead of being scheduled.
        """

        state = self._store.state()
        bag = state.bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        if bag.state is not BagState.RECHECKED:
            raise ConflictError(f"bag {bag_id!r} is not awaiting a recheck verdict")
        existing = state.retry_accounts.get(bag_id)
        if existing is not None and existing.state is RetryState.SCHEDULED:
            return RetryOutcome(existing, "duplicate")
        if existing is not None and existing.state is RetryState.MANUAL:
            return RetryOutcome(existing, "already_manual")
        policy = self._ensure_policy()
        attempts = existing.attempts if existing is not None else 0
        if attempts >= policy.max_attempts:
            return self._escalate(bag, attempts, ATTEMPTS_EXHAUSTED, "")
        backlog = self._recheck.length()
        if backlog >= policy.max_backlog:
            self._count("retry.throttled")
            raise QuotaError(
                f"recheck station backlog {backlog} reached the limit "
                f"{policy.max_backlog}; resubmission of {bag_id!r} is throttled"
            )
        flight_id = self._mapping.flight_of(bag_id)
        flight = self._book.get(flight_id)
        slot = self._slot(policy, flight.cutoff_at, backlog)
        if slot is None:
            return self._escalate(bag, attempts, NO_FEASIBLE_SLOT, flight.cutoff_at)
        account = RetryAccount(
            bag_id=bag_id,
            attempts=attempts,
            state=RetryState.SCHEDULED,
            slot=slot,
            cutoff_at=flight.cutoff_at,
            updated_at=self._clock.now_iso(),
        )
        with self._store.session() as session:
            session.state.retry_accounts[bag_id] = account
            # A scheduled bag waits for its slot, not for a verdict, so it
            # leaves the recheck queue and the two never overlap.
            if bag_id in session.state.recheck_queue:
                session.state.recheck_queue = [
                    item for item in session.state.recheck_queue if item != bag_id
                ]
                session.record("recheck.dequeue", {"bag_id": bag_id}, self._clock.now_iso())
            session.record(
                "retry.schedule",
                {
                    "account": account.as_dict(),
                    "attempt_no": attempts + 1,
                    "strategy": policy.strategy,
                    "backlog": backlog,
                },
                self._clock.now_iso(),
            )
            session.record(
                "counter.add", {"name": "retry.scheduled", "delta": 1}, self._clock.now_iso()
            )
        return RetryOutcome(account, "scheduled")

    def execute_due(self) -> list[RetryOutcome]:
        """Fire every scheduled resubmission whose slot has arrived."""

        return [self._execute(account) for account in self.due()]

    def execute(self, bag_id: str) -> RetryOutcome:
        """Fire one bag's resubmission when its slot has arrived.

        A bag with no due plan is left untouched, so repeated triggers are
        harmless.
        """

        account = self.account(bag_id)
        if account is None:
            raise unknown_bag(bag_id)
        if account.state is not RetryState.SCHEDULED:
            return RetryOutcome(account, "not_scheduled")
        if account.slot > self._clock.now_iso():
            return RetryOutcome(account, "not_due")
        return self._execute(account)

    def clear(self, bag_id: str) -> RetryOutcome:
        """Close the ledger entry of a bag that passed recheck.

        The attempt history is kept; only the lifecycle state moves.
        """

        account = self.account(bag_id)
        if account is None:
            raise unknown_bag(bag_id)
        if account.state is RetryState.CLEARED:
            return RetryOutcome(account, "already_cleared")
        cleared = account.evolved(
            state=RetryState.CLEARED, slot="", updated_at=self._clock.now_iso()
        )
        with self._store.session() as session:
            session.state.retry_accounts[bag_id] = cleared
            session.record("retry.clear", {"account": cleared.as_dict()}, self._clock.now_iso())
            session.record(
                "counter.add", {"name": "retry.cleared", "delta": 1}, self._clock.now_iso()
            )
        return RetryOutcome(cleared, "cleared")

    # -- internals -------------------------------------------------------------

    def _execute(self, account: RetryAccount) -> RetryOutcome:
        policy = self._ensure_policy()
        bag = self._store.state().bags.get(account.bag_id)
        if bag is None:
            raise unknown_bag(account.bag_id)
        flight_id = self._mapping.flight_of(account.bag_id)
        flight = self._book.get(flight_id)
        # Re-validate the window: the schedule may have moved, or the station
        # may have run late, since the slot was planned. A lapsed slot
        # escalates instead of firing a resubmission that cannot make it.
        if flight.cutoff_at:
            latest = _parse_cutoff(flight.cutoff_at) - timedelta(
                seconds=policy.min_cycle_seconds
            )
            if self._clock.now() > latest:
                return self._escalate(bag, account.attempts, SLOT_LAPSED, flight.cutoff_at)
        if not is_open_state(flight.state):
            return self._escalate(bag, account.attempts, FLIGHT_CLOSED, flight.cutoff_at)
        executed = account.evolved(
            attempts=account.attempts + 1,
            state=RetryState.IN_TRANSIT,
            slot="",
            updated_at=self._clock.now_iso(),
        )
        injected = bag.evolved(state=BagState.INJECTED)
        with self._store.session() as session:
            session.state.retry_accounts[account.bag_id] = executed
            session.state.bags[bag.id] = injected
            session.record(
                "retry.execute",
                {
                    "account": executed.as_dict(),
                    "attempt_no": executed.attempts,
                    "planned_slot": account.slot,
                    "quota_consumed": 0,
                },
                self._clock.now_iso(),
            )
            session.record("bag.put", {"bag": injected.as_dict()}, self._clock.now_iso())
            session.record(
                "counter.add", {"name": "retry.executed", "delta": 1}, self._clock.now_iso()
            )
        return RetryOutcome(executed, "executed")

    def _escalate(
        self, bag: Bag, attempts: int, reason: str, cutoff_at: str
    ) -> RetryOutcome:
        """Divert a bag to manual handling, recording where and why it stuck."""

        account = RetryAccount(
            bag_id=bag.id,
            attempts=attempts,
            state=RetryState.MANUAL,
            slot="",
            cutoff_at=cutoff_at,
            reason=reason,
            stuck_stage=STUCK_STAGE,
            updated_at=self._clock.now_iso(),
        )
        diverted = bag.evolved(state=BagState.DIVERTED)
        with self._store.session() as session:
            session.state.retry_accounts[bag.id] = account
            session.state.bags[bag.id] = diverted
            if bag.id in session.state.recheck_queue:
                session.state.recheck_queue = [
                    item for item in session.state.recheck_queue if item != bag.id
                ]
                session.record("recheck.dequeue", {"bag_id": bag.id}, self._clock.now_iso())
            session.record("retry.escalate", {"account": account.as_dict()}, self._clock.now_iso())
            session.record("bag.put", {"bag": diverted.as_dict()}, self._clock.now_iso())
            session.record(
                "counter.add", {"name": "retry.escalated", "delta": 1}, self._clock.now_iso()
            )
        return RetryOutcome(account, "escalated")

    def _slot(self, policy: RetryPolicy, cutoff_at: str, backlog: int) -> str | None:
        """The next resubmission instant, or ``None`` when no slot fits.

        The earliest feasible instant is pushed out by the retry spacing and by
        the time the station needs to work through its current backlog; the two
        overlap, so the later one wins. Anchored to the cutoff, the slot must
        also leave the bag at least one minimum line cycle before the flight
        closes. A flight with no cutoff on record degenerates to plain spacing.
        """

        spacing = max(
            policy.interval_seconds, backlog * policy.service_time_seconds
        )
        earliest = self._clock.now() + timedelta(seconds=spacing)
        if not cutoff_at:
            return _format(earliest)
        latest = _parse_cutoff(cutoff_at) - timedelta(seconds=policy.min_cycle_seconds)
        if earliest > latest:
            return None
        return _format(earliest)

    def _count(self, name: str) -> None:
        with self._store.session() as session:
            session.record(
                "counter.add", {"name": name, "delta": 1}, self._clock.now_iso()
            )
