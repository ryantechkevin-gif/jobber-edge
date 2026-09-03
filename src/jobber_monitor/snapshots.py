"""
Weekly snapshot storage for the Monday Dashboard's week-over-week
comparison -- stored in the same Azure Blob container already used for
OAuth token state (see state_store.py), keyed by the report's week-ending
date, so "last week's numbers" are real stored data instead of something
a chat session has to remember.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from . import state_store

_BLOB_PREFIX = "monday-dashboard/snapshot-"


def _blob_name(week_ending: date) -> str:
    return f"{_BLOB_PREFIX}{week_ending.isoformat()}.json"


def save_snapshot(week_ending: date, snapshot: Dict[str, Any]) -> None:
    state_store.save_json(_blob_name(week_ending), snapshot)


def load_snapshot(week_ending: date) -> Optional[Dict[str, Any]]:
    return state_store.load_json(_blob_name(week_ending))


def load_previous_snapshot(week_ending: date) -> Optional[Dict[str, Any]]:
    return load_snapshot(week_ending - timedelta(days=7))


def build_snapshot(active_recurring_jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    `active_recurring_jobs` is the active-only output of
    ask.py's _list_recurring_jobs() / the equivalent monday_dashboard
    fetch -- dicts already carrying job_id/client_id/client_name/
    company_name/job_number/monthly_total.
    """
    jobs_by_id = {
        j["job_id"]: {
            "client_name": j.get("client_name"),
            "company_name": j.get("company_name"),
            "job_number": j.get("job_number"),
            "monthly_total": j.get("monthly_total"),
        }
        for j in active_recurring_jobs
    }
    client_ids = {j.get("client_id") for j in active_recurring_jobs if j.get("client_id")}
    return {
        "active_recurring_jobs": jobs_by_id,
        "mrr_total": round(sum((j.get("monthly_total") or 0) for j in active_recurring_jobs), 2),
        "active_client_count": len(client_ids),
    }


def diff_snapshots(current: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compares two build_snapshot() payloads by job id -- precise enough to
    say exactly which jobs are new/closed, not just a raw count delta.
    """
    if previous is None:
        return {
            "has_previous": False,
            "new_recurring_jobs": [],
            "closed_recurring_jobs": [],
            "mrr_change": None,
            "client_count_change": None,
        }

    cur_jobs = current.get("active_recurring_jobs", {})
    prev_jobs = previous.get("active_recurring_jobs", {})

    new_ids = set(cur_jobs) - set(prev_jobs)
    closed_ids = set(prev_jobs) - set(cur_jobs)

    return {
        "has_previous": True,
        "new_recurring_jobs": [{"job_id": jid, **cur_jobs[jid]} for jid in new_ids],
        "closed_recurring_jobs": [{"job_id": jid, **prev_jobs[jid]} for jid in closed_ids],
        "mrr_change": round(current.get("mrr_total", 0) - previous.get("mrr_total", 0), 2),
        "client_count_change": current.get("active_client_count", 0) - previous.get("active_client_count", 0),
    }
