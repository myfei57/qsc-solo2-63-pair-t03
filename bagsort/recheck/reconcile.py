"""Cross-checks between the recheck queue, the retry ledger and the books.

Every check recomputes one ledger independently and compares it against
another, so a drift between what the queue shows, what the retry ledger
claims, what the sorter recorded and what the quota book charged surfaces as a
named failed check instead of a quiet inconsistency.
"""

from __future__ import annotations

from typing import Any

from bagsort.model.enums import BagState, RetryState
from bagsort.model.state import State
from bagsort.store.journal import JournalEntry
from bagsort.store.store import Store

SETTLED_STATES = (BagState.SORTED, BagState.LOADED, BagState.DIVERTED)


class RetryReconciler:
    """Verifies the invariants that tie the four ledgers together."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def report(self) -> dict[str, Any]:
        state = self._store.state()
        entries = self._store.entries()
        checks = [
            self._ledger_matches_journal(state, entries),
            self._queue_disjoint_from_schedule(state),
            self._no_pending_for_settled(state),
            self._manual_means_diverted(state),
            self._quota_fold_consistent(state, entries),
            self._retries_declare_zero_quota(entries),
        ]
        counts: dict[str, int] = {}
        for account in state.retry_accounts.values():
            counts[account.state.value] = counts.get(account.state.value, 0) + 1
        return {
            "ok": all(check["ok"] for check in checks),
            "checks": checks,
            "accounts": counts,
            "attempts": sum(
                account.attempts for account in state.retry_accounts.values()
            ),
            "executed_commands": sum(
                1 for entry in entries if entry.kind == "retry.execute"
            ),
        }

    # -- individual checks -----------------------------------------------------

    def _ledger_matches_journal(
        self, state: State, entries: list[JournalEntry]
    ) -> dict[str, Any]:
        """Attempts on the ledger equal the executed commands in the journal."""

        executed: dict[str, int] = {}
        for entry in entries:
            if entry.kind == "retry.execute":
                bag_id = str(entry.payload["account"]["bag_id"])
                executed[bag_id] = executed.get(bag_id, 0) + 1
        drifted = sorted(
            bag_id
            for bag_id, account in state.retry_accounts.items()
            if account.attempts != executed.get(bag_id, 0)
        )
        drifted += sorted(
            bag_id for bag_id in executed if bag_id not in state.retry_accounts
        )
        return _check(
            "ledger_matches_journal",
            not drifted,
            f"attempt count differs from the journal for {drifted}" if drifted else "",
        )

    def _queue_disjoint_from_schedule(self, state: State) -> dict[str, Any]:
        """A bag waiting for its slot never also waits in the recheck queue."""

        scheduled = {
            bag_id
            for bag_id, account in state.retry_accounts.items()
            if account.state is RetryState.SCHEDULED
        }
        overlap = sorted(scheduled.intersection(state.recheck_queue))
        return _check(
            "queue_disjoint_from_schedule",
            not overlap,
            f"bags both queued and scheduled {overlap}" if overlap else "",
        )

    def _no_pending_for_settled(self, state: State) -> dict[str, Any]:
        """A settled bag (sorted, loaded, diverted) has no pending resubmission."""

        offenders = sorted(
            bag_id
            for bag_id, account in state.retry_accounts.items()
            if account.state is RetryState.SCHEDULED
            and bag_id in state.bags
            and state.bags[bag_id].state in SETTLED_STATES
        )
        return _check(
            "no_pending_for_settled",
            not offenders,
            f"settled bags still scheduled {offenders}" if offenders else "",
        )

    def _manual_means_diverted(self, state: State) -> dict[str, Any]:
        """Every bag escalated to manual handling was physically diverted."""

        offenders = sorted(
            bag_id
            for bag_id, account in state.retry_accounts.items()
            if account.state is RetryState.MANUAL
            and bag_id in state.bags
            and state.bags[bag_id].state is not BagState.DIVERTED
        )
        return _check(
            "manual_means_diverted",
            not offenders,
            f"manual bags not diverted {offenders}" if offenders else "",
        )

    def _quota_fold_consistent(
        self, state: State, entries: list[JournalEntry]
    ) -> dict[str, Any]:
        """The allowance book refolds from its own commands without drift."""

        folded: dict[str, int] = {}
        for entry in entries:
            if entry.kind == "quota.set":
                folded[str(entry.payload["flight_id"])] = int(entry.payload["limit"])
            elif entry.kind == "quota.adjust":
                flight_id = str(entry.payload["flight_id"])
                folded[flight_id] = folded.get(flight_id, 0) + int(entry.payload["delta"])
        drifted = sorted(
            flight_id
            for flight_id in set(folded) | set(state.quota_limits)
            if folded.get(flight_id) != state.quota_limits.get(flight_id)
        )
        return _check(
            "quota_fold_consistent",
            not drifted,
            f"allowance drift for {drifted}" if drifted else "",
        )

    def _retries_declare_zero_quota(
        self, entries: list[JournalEntry]
    ) -> dict[str, Any]:
        """No resubmission charged the flight allowance a second time."""

        offenders = [
            entry.sequence
            for entry in entries
            if entry.kind == "retry.execute"
            and int(entry.payload.get("quota_consumed", 0)) != 0
        ]
        return _check(
            "retries_declare_zero_quota",
            not offenders,
            f"retry commands that consumed allowance {offenders}" if offenders else "",
        )


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}
