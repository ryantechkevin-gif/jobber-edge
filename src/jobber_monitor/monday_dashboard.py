"""
Monday Dashboard -- the live-API replacement for the manually-exported
5-CSV weekly report (recurring jobs / one-off jobs / clients / invoices /
WeeFee export) that used to require a Claude session on someone's Mac to
assemble by hand. Every section here is a deterministic Python function
over data already fetched through lookups.py, not LLM-derived -- the
numbers need to be reproducible and checkable, not regenerated slightly
differently each time.

Sections deliberately NOT handled here: WeeFee/Receptionist activity has
no API exposure at all (confirmed live via introspection of the Query
root type -- no conversations/calls/texts/receptionist fields anywhere),
so that section is merged in separately from whatever export/paste is
available that week. See function_app.py's monday-dashboard route.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from . import lookups, snapshots

_GHOST_STATUS = "paid"
_PAST_DUE_STATUS = "past_due"


def _last_completed_week_end(run_date: date) -> date:
    """
    The most recently completed Sunday strictly before run_date. This
    runs Monday mornings, so for a Monday run_date that's simply
    yesterday -- written generally so backtesting against an arbitrary
    date, or a late/manual run, still lands on a sensible week.
    """
    days_since_sunday = (run_date.weekday() - 6) % 7  # Mon->1 ... Sat->6, Sun->0
    if days_since_sunday == 0:
        days_since_sunday = 7
    return run_date - timedelta(days=days_since_sunday)


# ---------- section builders (pure functions over already-fetched data) ----------

def monthly_recurring_section(active_recurring_jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    client_ids = {j["client_id"] for j in active_recurring_jobs if j.get("client_id")}
    rows = sorted(active_recurring_jobs, key=lambda j: j.get("monthly_total") or 0, reverse=True)
    return {
        "job_count": len(active_recurring_jobs),
        "client_count": len(client_ids),
        "mrr_total": round(sum((j.get("monthly_total") or 0) for j in active_recurring_jobs), 2),
        "jobs": rows,
    }


def week_over_week_section(active_recurring_jobs: List[Dict[str, Any]], week_end: date) -> Dict[str, Any]:
    current = snapshots.build_snapshot(active_recurring_jobs)
    previous = snapshots.load_previous_snapshot(week_end)
    diff = snapshots.diff_snapshots(current, previous)
    return {**diff, "current_snapshot": current}


def billing_risk_section(invoices: List[Dict[str, Any]]) -> Dict[str, Any]:
    outstanding = [i for i in invoices if (i.get("balance") or 0) != 0]
    ghost = [i for i in outstanding if (i.get("status") or "").lower() == _GHOST_STATUS]
    ghost_ids = {i["invoice_id"] for i in ghost}
    real_outstanding = [i for i in outstanding if i["invoice_id"] not in ghost_ids]
    past_due = [i for i in real_outstanding if (i.get("status") or "").lower() == _PAST_DUE_STATUS]

    by_client: Dict[str, Dict[str, Any]] = {}
    for inv in real_outstanding:
        key = inv.get("client_name") or "Unknown"
        entry = by_client.setdefault(key, {"client_name": key, "invoices": [], "total_balance": 0.0})
        entry["invoices"].append(inv)
        entry["total_balance"] = round(entry["total_balance"] + (inv.get("balance") or 0), 2)

    ghost_client_names = {i.get("client_name") or "Unknown" for i in ghost}
    all_client_names_with_balance = ghost_client_names | set(by_client.keys())

    return {
        "total_outstanding": round(sum((i.get("balance") or 0) for i in outstanding), 2),
        "clients_with_balance_count": len(all_client_names_with_balance),
        "past_due_count": len(past_due),
        "past_due_invoices": past_due,
        "ghost_balance_total": round(sum((i.get("balance") or 0) for i in ghost), 2),
        "ghost_balance_invoice_count": len(ghost),
        "ghost_balance_invoices": ghost,
        "clients_with_real_balance": sorted(by_client.values(), key=lambda c: c["total_balance"], reverse=True),
    }


def incomplete_jobs_section(one_off_jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    incomplete = [
        j for j in one_off_jobs
        if j.get("completed_at") is None and (j.get("job_status") or "").lower() != "archived"
    ]
    incomplete.sort(key=lambda j: j.get("start_at") or "")
    return {"count": len(incomplete), "jobs": incomplete}


def paid_this_week_section(invoices: List[Dict[str, Any]], week_start: date, week_end: date) -> Dict[str, Any]:
    payments = []
    for inv in invoices:
        for pr in inv.get("payment_records", []):
            when = lookups.parse_date(pr.get("entry_date"))
            if when and week_start <= when <= week_end:
                payments.append({
                    "invoice_id": inv["invoice_id"],
                    "invoice_number": inv.get("invoice_number"),
                    "client_name": inv.get("client_name"),
                    "amount": pr.get("amount"),
                    "marked_paid": pr.get("entry_date"),
                    "jobber_web_uri": inv.get("jobber_web_uri"),
                })
    payments.sort(key=lambda p: p["marked_paid"] or "", reverse=True)
    return {
        "count": len(payments),
        "collected_total": round(sum((p.get("amount") or 0) for p in payments), 2),
        "payments": payments,
    }


def visits_this_week_section(visits: List[Dict[str, Any]], week_start: date, week_end: date) -> Dict[str, Any]:
    in_week = []
    for v in visits:
        when = lookups.parse_date(v.get("start_at"))
        if when and week_start <= when <= week_end:
            in_week.append(v)
    in_week.sort(key=lambda v: v.get("start_at") or "")
    return {"count": len(in_week), "visits": in_week}


def new_client_eval_section(
    one_off_jobs: List[Dict[str, Any]],
    active_recurring_client_ids: Set[str],
    clients_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    by_client: Dict[str, Dict[str, Any]] = {}
    for j in one_off_jobs:
        cid = j.get("client_id")
        if not cid or cid in active_recurring_client_ids:
            continue
        entry = by_client.setdefault(cid, {
            "client_id": cid,
            "client_name": j.get("client_name"),
            "company_name": j.get("company_name"),
            "emails": (clients_by_id.get(cid) or {}).get("emails") or [],
            "total_paid": 0.0,
        })
        entry["total_paid"] = round(entry["total_paid"] + (j.get("total") or 0), 2)
    rows = sorted(by_client.values(), key=lambda c: c["total_paid"], reverse=True)
    return {"count": len(rows), "clients": rows}


def invoice_status_summary_section(invoices: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Counter = Counter()
    totals: Counter = Counter()
    for inv in invoices:
        status = inv.get("status") or "unknown"
        counts[status] += 1
        totals[status] = round(totals[status] + (inv.get("total") or 0), 2)
    return {"counts": dict(counts), "totals": dict(totals)}


def priority_actions_section(
    active_recurring_jobs: List[Dict[str, Any]],
    billing_risk: Dict[str, Any],
    incomplete_jobs: Dict[str, Any],
) -> Dict[str, Any]:
    actions: List[Dict[str, Any]] = []

    for j in active_recurring_jobs:
        if j.get("autopay_enabled") is False:
            actions.append({
                "type": "autopay_disabled",
                "client_name": j.get("client_name"),
                "issue": f"Autopay Disabled -- {j.get('title') or 'Recurring job'}",
                "job_number": j.get("job_number"),
                "amount": j.get("monthly_total"),
                "jobber_web_uri": j.get("jobber_web_uri"),
            })

    for inv in billing_risk.get("past_due_invoices", []):
        actions.append({
            "type": "past_due_invoice",
            "client_name": inv.get("client_name"),
            "issue": f"Past due invoice #{inv.get('invoice_number')}",
            "amount": inv.get("balance"),
            "jobber_web_uri": inv.get("jobber_web_uri"),
        })

    for j in incomplete_jobs.get("jobs", []):
        if (j.get("uninvoiced_total") or 0) > 0:
            actions.append({
                "type": "incomplete_job_uninvoiced",
                "client_name": j.get("client_name"),
                "issue": f"Incomplete job with uninvoiced total -- {j.get('title') or 'Untitled job'}",
                "job_number": j.get("job_number"),
                "amount": j.get("uninvoiced_total"),
                "jobber_web_uri": j.get("jobber_web_uri"),
            })

    return {"count": len(actions), "actions": actions}


# ---------- WeeFee / AI Receptionist (no API exposure -- merged in
# externally, see the module docstring) ----------

_WEEFEE_DEFAULTS: Dict[str, Any] = {
    "total_conversations": 0,
    "calls": 0,
    "texts": 0,
    "web_chats": 0,
    "needs_attention": 0,
    "handled_no_action": 0,
    "resolved": 0,
    "rolling": {},
    "conversations": [],
}


def normalize_weefee(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    `payload` is whatever shape that week's WeeFee export/paste produces
    -- format isn't nailed down yet, so this just fills in zero/empty
    defaults for anything missing rather than assuming a fixed schema,
    so the rest of the report still renders with partial or no WeeFee
    data for a given week.
    """
    if not payload:
        return {"provided": False, **_WEEFEE_DEFAULTS}
    merged = {**_WEEFEE_DEFAULTS, **payload}
    merged["provided"] = True
    return merged


# ---------- orchestration ----------

def build_monday_dashboard(week_end: Optional[date] = None, weefee: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    run_date = datetime.now(lookups.BUSINESS_TZ).date()
    week_end = week_end or _last_completed_week_end(run_date)
    week_start = week_end - timedelta(days=6)

    all_recurring = lookups.list_recurring_jobs(active_only=False)
    active_recurring = [j for j in all_recurring if (j.get("job_status") or "").lower() != "archived"]
    closed_recurring = [j for j in all_recurring if (j.get("job_status") or "").lower() == "archived"]

    one_off_jobs = lookups.list_jobs(job_type="ONE_OFF")
    # Unbounded -- billing risk/invoice status want the full picture, not
    # just this week, and paid_this_week filters by payment entry_date
    # (which can be well after an invoice's own issued date) not by
    # issuedDate, so restricting the fetch window here would silently
    # miss old invoices paid this week.
    invoices = lookups.list_invoices(since_date=None, until_date=None)
    visits = lookups.list_visits()
    clients_by_id = {c["client_id"]: c for c in lookups.list_clients()}

    billing_risk = billing_risk_section(invoices)
    incomplete = incomplete_jobs_section(one_off_jobs)
    active_recurring_client_ids = {j["client_id"] for j in active_recurring if j.get("client_id")}

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "generated_at": run_date.isoformat(),
        "priority_actions": priority_actions_section(active_recurring, billing_risk, incomplete),
        "monthly_recurring": monthly_recurring_section(active_recurring),
        "week_over_week": week_over_week_section(active_recurring, week_end),
        "billing_risk": billing_risk,
        "incomplete_jobs": incomplete,
        "paid_this_week": paid_this_week_section(invoices, week_start, week_end),
        "visits_this_week": visits_this_week_section(visits, week_start, week_end),
        "new_client_eval": new_client_eval_section(one_off_jobs, active_recurring_client_ids, clients_by_id),
        "closed_recurring": {"count": len(closed_recurring), "jobs": closed_recurring},
        "invoice_status_summary": invoice_status_summary_section(invoices),
        "weefee": normalize_weefee(weefee),
    }


def save_this_weeks_snapshot(dashboard: Dict[str, Any]) -> None:
    week_end = date.fromisoformat(dashboard["week_end"])
    snapshots.save_snapshot(week_end, dashboard["week_over_week"]["current_snapshot"])
