"""Human readable and structured summaries of the whole plant."""

from __future__ import annotations

from bagsort.model.validate import is_active_bag_state, is_open_state


class PlantReport:
    """Pulls one consistent picture out of the subsystems on demand."""

    def __init__(
        self,
        store: object,
        book: object,
        chutes: object,
        belt: object,
        quota: object,
        audit: object,
        namespace: object,
        topology: object,
        reinject: object | None = None,
        reinject_quota: object | None = None,
    ) -> None:
        self._store = store
        self._book = book
        self._chutes = chutes
        self._belt = belt
        self._quota = quota
        self._audit = audit
        self._ns = namespace
        self._topology = topology
        self._reinject = reinject
        self._reinject_quota = reinject_quota

    def summary(self) -> dict[str, object]:
        flights = self._book.list()
        bags = list(self._store.state().bags.values())
        summary = {
            "terminal": self._ns.terminal,
            "line": list(self._topology.stages),
            "flights": len(flights),
            "open_flights": sum(1 for flight in flights if is_open_state(flight.state)),
            "bags": len(bags),
            "active_bags": sum(1 for bag in bags if is_active_bag_state(bag.state)),
            "manual_handling": sum(
                1 for bag in bags if bag.state.value == "manual_handling"
            ),
            "assignments": self._chutes.count(),
            "sort_records": len(self._store.state().sort_records),
            "audit_entries": self._audit.count(),
            "audit_intact": self._audit.verify(),
            "carousel": self._belt.carousel(),
            "batch": self._belt.batch(),
            "watermark": self._store.watermark,
            "quota": self._quota.limits(),
        }
        if self._reinject is not None:
            plans = self._reinject.plans()
            summary.update(
                {
                    "reinject_plans": len(plans),
                    "reinject_injected": sum(plan.used_attempts for plan in plans),
                    "manual_handoffs": len(self._reinject.handoffs()),
                    "recheck_capacity": self._reinject.recheck_capacity(),
                }
            )
        if self._reinject_quota is not None:
            summary["reinject_quota"] = self._reinject_quota.limits()
        return summary

    def flight_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for flight in self._book.list():
            assignment = self._chutes.current(flight.id)
            rows.append(
                {
                    "id": flight.id,
                    "code": flight.code,
                    "state": flight.state.value,
                    "carousel": flight.carousel,
                    "batch": flight.batch,
                    "cutoff_at": flight.cutoff_at,
                    "schedule_generation": flight.schedule_generation,
                    "chute_id": assignment.chute_id if assignment else "",
                    "chute_generation": assignment.generation if assignment else 0,
                    "bags": len(self._book.list_bags_for(flight.id)),
                }
            )
        return rows

    def text(self) -> str:
        summary = self.summary()
        lines = [
            f"terminal          : {summary['terminal']}",
            f"line              : {' -> '.join(summary['line'])}",
            f"flights           : {summary['flights']} ({summary['open_flights']} open)",
            f"bags              : {summary['bags']} ({summary['active_bags']} active)",
            f"manual desk       : {summary['manual_handling']}",
            f"chute assignments : {summary['assignments']}",
            f"sort records      : {summary['sort_records']}",
            f"audit entries     : {summary['audit_entries']} intact={summary['audit_intact']}",
            f"carousel          : {summary['carousel']} batch={summary['batch']}",
            f"journal watermark : {summary['watermark']}",
        ]
        if "reinject_plans" in summary:
            lines.append(
                f"reinjection       : {summary['reinject_injected']} injected / "
                f"{summary['reinject_plans']} plans / "
                f"{summary['manual_handoffs']} handoffs / "
                f"capacity={summary['recheck_capacity']}"
            )
        return "\n".join(lines) + "\n"
