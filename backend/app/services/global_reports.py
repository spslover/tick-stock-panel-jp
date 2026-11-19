"""JP & US market/stock AI report persistence backed by JsonReportStore."""
from __future__ import annotations

from app.services.json_report_store import JsonReportStore

MAX_REPORTS = 50

_store = JsonReportStore("ai_global_reports.json", MAX_REPORTS, id_prefix="gar")


def list_reports() -> list[dict]:
    """Return all saved JP/US AI analysis reports (descending by created_at)."""
    return _store.list_reports()


def save_report(report: dict) -> dict:
    """Persist a JP/US AI analysis report and return the saved record."""
    return _store.save_report(report)


def delete_report(report_id: str) -> bool:
    """Delete a saved JP/US AI analysis report by id."""
    return _store.delete_report(report_id)
