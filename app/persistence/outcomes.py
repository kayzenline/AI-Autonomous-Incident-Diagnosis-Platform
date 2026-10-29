"""Which run statuses must keep a report.

BUDGET_EXCEEDED and TIMED_OUT store a partial report and are scored.
CANCELLED stores none. FAILED may store none, including a restart.
"""

NEEDS_REPORT = frozenset({"COMPLETED", "TIMED_OUT", "BUDGET_EXCEEDED"})
ACTIVE = frozenset({"PENDING", "RUNNING"})


def validate_outcome(status: str, report: dict[str, object] | None) -> None:
    if status in NEEDS_REPORT and report is None:
        raise ValueError(f"{status} requires a final_report")
    if status == "CANCELLED" and report is not None:
        raise ValueError("CANCELLED does not store a report")
