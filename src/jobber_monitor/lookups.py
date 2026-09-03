"""
Shared read-only Jobber lookups -- used by both the ask.py Q&A toolbelt
and monday_dashboard.py's section builders, so the two features compute
"active recurring jobs" / "invoices in a date range" / etc. the exact
same way rather than each having its own (possibly diverging) copy.

Every function here does its own filtering/sorting/aggregation in Python
against data pulled through execute()/paginate(), rather than trusting
Jobber's own (limited, inconsistently available) filter/sort arguments --
e.g. Invoice/Quote nest `total` under `amounts` rather than as a flat
field, and jobStatus is a UI bucket (active/late/today/upcoming/
action_required/on_hold/unscheduled/expiring_within_30_days/
requires_invoicing/archived) where 'archived' is the only closed value,
not a simple active/archived flag (confirmed live: one real page of
recurring jobs was 85% "action_required" and only 14% "active").
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .jobber_client import paginate
from .queries import CLIENTS_FULL_QUERY, CLIENTS_QUERY, INVOICES_QUERY, JOBS_QUERY, VISITS_QUERY
from .report import fetch_client_dashboard

BUSINESS_TZ = ZoneInfo("America/Phoenix")


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# Cached for a short window so a batch of calls in quick succession (a
# single ask.py question triggering several tool calls, or one
# monday_dashboard run needing the same client set for multiple sections)
# doesn't re-fetch every client from Jobber each time -- correctness
# doesn't depend on this (recurring billing data doesn't change
# second-to-second), it's purely to keep Jobber round trips down.
_cache: Dict[str, Any] = {"clients_full": None, "clients_full_at": 0.0}
_CACHE_TTL_SECONDS = 60


# Used for property/custom-field search and anything needing a client's
# properties, not for recurring-job questions -- those go through
# list_recurring_jobs() instead (root-level, filterable, confirmed live
# via a before/after comparison against this per-client walk to return
# identical job records).
def all_clients_full() -> List[Dict[str, Any]]:
    now = time.time()
    if _cache["clients_full"] is not None and (now - _cache["clients_full_at"]) < _CACHE_TTL_SECONDS:
        return _cache["clients_full"]
    clients = paginate(CLIENTS_FULL_QUERY, ["clients"])
    _cache["clients_full"] = clients
    _cache["clients_full_at"] = now
    return clients


def custom_field_value(field: Dict[str, Any]) -> Any:
    return field.get("valueText") if field.get("valueText") is not None else field.get("valueDropdown")


def property_summaries(client: Dict[str, Any]) -> List[Dict[str, Any]]:
    props = []
    for p in (client.get("clientProperties") or {}).get("nodes", []):
        addr = p.get("address") or {}
        fields = {
            f.get("label"): custom_field_value(f)
            for f in (p.get("customFields") or [])
            if custom_field_value(f) not in (None, "")
        }
        props.append({
            "property_id": p.get("id"),
            "name": p.get("name"),
            "address": ", ".join(filter(None, [addr.get("street"), addr.get("city"), addr.get("province"), addr.get("postalCode")])),
            "custom_fields": fields,
        })
    return props


def job_summaries(client: Dict[str, Any], recurring_only: bool, active_only: bool) -> List[Dict[str, Any]]:
    out = []
    for j in (client.get("jobs") or {}).get("nodes", []):
        if recurring_only and j.get("jobType") != "RECURRING":
            continue
        if active_only and (j.get("jobStatus") or "").lower() == "archived":
            continue
        out.append({
            "job_id": j["id"],
            "job_number": j.get("jobNumber"),
            "title": j.get("title"),
            "job_type": j.get("jobType"),
            "job_status": j.get("jobStatus"),
            "monthly_total": j.get("total"),
            "invoiced_total": j.get("invoicedTotal"),
            "uninvoiced_total": j.get("uninvoicedTotal"),
            "start_at": j.get("startAt"),
            "end_at": j.get("endAt"),
            "jobber_web_uri": j.get("jobberWebUri"),
        })
    return out


def list_jobs(job_type: Optional[str] = None, active_only: bool = False) -> List[Dict[str, Any]]:
    """
    job_type: 'RECURRING', 'ONE_OFF', or None for every job. active_only
    excludes jobs whose jobStatus is 'archived' -- see the JOBS_QUERY
    comment for why that's the right "still ongoing" check, not
    jobStatus == 'active'.
    """
    filter_ = {"jobType": job_type} if job_type else None
    jobs = paginate(JOBS_QUERY, ["jobs"], {"filter": filter_})
    out = []
    for j in jobs:
        if active_only and (j.get("jobStatus") or "").lower() == "archived":
            continue
        client = j.get("client") or {}
        out.append({
            "client_id": client.get("id"),
            "client_name": client.get("name"),
            "company_name": client.get("companyName"),
            "client_archived": client.get("isArchived"),
            "job_id": j["id"],
            "job_number": j.get("jobNumber"),
            "title": j.get("title"),
            "job_type": j.get("jobType"),
            "job_status": j.get("jobStatus"),
            "total": j.get("total"),
            "invoiced_total": j.get("invoicedTotal"),
            "uninvoiced_total": j.get("uninvoicedTotal"),
            "start_at": j.get("startAt"),
            "end_at": j.get("endAt"),
            "completed_at": j.get("completedAt"),
            "autopay_enabled": j.get("willClientBeAutomaticallyCharged"),
            "jobber_web_uri": j.get("jobberWebUri"),
        })
    return out


def list_recurring_jobs(active_only: bool = True) -> List[Dict[str, Any]]:
    jobs = list_jobs(job_type="RECURRING", active_only=active_only)
    for j in jobs:
        j["monthly_total"] = j.pop("total")
        j.pop("completed_at", None)
    return jobs


def list_invoices(
    since_date: Optional[str] = None,
    until_date: Optional[str] = None,
    recurring_only: bool = False,
) -> List[Dict[str, Any]]:
    invoices = paginate(INVOICES_QUERY, ["invoices"])
    since = parse_date(since_date)
    until = parse_date(until_date) or datetime.now(BUSINESS_TZ).date()

    out = []
    for inv in invoices:
        when = parse_date(inv.get("issuedDate")) or parse_date(inv.get("createdAt"))
        if when is None:
            continue
        if since and when < since:
            continue
        if when > until:
            continue

        linked_jobs = (inv.get("jobs") or {}).get("nodes", [])
        is_recurring = any(j.get("jobType") == "RECURRING" for j in linked_jobs)
        if recurring_only and not is_recurring:
            continue

        amounts = inv.get("amounts") or {}
        client = inv.get("client") or {}
        out.append({
            "invoice_id": inv["id"],
            "invoice_number": inv.get("invoiceNumber"),
            "status": inv.get("invoiceStatus"),
            "subject": inv.get("subject"),
            "issued_date": inv.get("issuedDate") or inv.get("createdAt"),
            "due_date": inv.get("dueDate"),
            "total": amounts.get("total"),
            "balance": amounts.get("invoiceBalance"),
            "client_name": client.get("name"),
            "company_name": client.get("companyName"),
            "is_recurring": is_recurring,
            "job_titles": [j.get("title") for j in linked_jobs],
            "payment_records": [
                {"payment_id": pr.get("id"), "amount": pr.get("amount"), "entry_date": pr.get("entryDate")}
                for pr in (inv.get("paymentRecords") or {}).get("nodes", [])
            ],
            "jobber_web_uri": inv.get("jobberWebUri"),
        })
    out.sort(key=lambda i: i["issued_date"] or "", reverse=True)
    return out


def search_clients_and_jobs(term: str) -> List[Dict[str, Any]]:
    needle = term.strip().lower()
    matches = []
    for c in all_clients_full():
        props = property_summaries(c)
        job_titles = [j.get("title") or "" for j in (c.get("jobs") or {}).get("nodes", [])]
        haystack = " ".join([
            c.get("name") or "", c.get("companyName") or "",
            *(p["name"] or "" for p in props),
            *(p["address"] or "" for p in props),
            *(f"{k} {v}" for p in props for k, v in p["custom_fields"].items()),
            *job_titles,
        ]).lower()
        if needle not in haystack:
            continue
        matches.append({
            "client_id": c["id"],
            "client_name": c.get("name"),
            "company_name": c.get("companyName"),
            "archived": c.get("isArchived"),
            "properties": props,
            "jobs": job_summaries(c, recurring_only=False, active_only=False),
        })
    return matches


def list_clients(active_only: bool = False) -> List[Dict[str, Any]]:
    clients = paginate(CLIENTS_QUERY, ["clients"])
    out = []
    for c in clients:
        if active_only and c.get("isArchived"):
            continue
        out.append({
            "client_id": c["id"],
            "name": c.get("name"),
            "is_company": c.get("isCompany"),
            "archived": c.get("isArchived"),
            "created_at": c.get("createdAt"),
            "emails": [e.get("address") for e in (c.get("emails") or [])],
            "phones": [p.get("number") for p in (c.get("phones") or [])],
        })
    return out


def list_visits() -> List[Dict[str, Any]]:
    visits = paginate(VISITS_QUERY, ["visits"])
    out = []
    for v in visits:
        client = v.get("client") or {}
        job = v.get("job") or {}
        prop = v.get("property") or {}
        addr = prop.get("address") or {}
        out.append({
            "visit_id": v["id"],
            "title": v.get("title"),
            "start_at": v.get("startAt"),
            "end_at": v.get("endAt"),
            "is_complete": v.get("isComplete"),
            "visit_status": v.get("visitStatus"),
            "client_name": client.get("name"),
            "company_name": client.get("companyName"),
            "job_id": job.get("id"),
            "job_number": job.get("jobNumber"),
            "job_title": job.get("title"),
            "address": ", ".join(filter(None, [addr.get("street"), addr.get("city"), addr.get("province"), addr.get("postalCode")])),
            "assigned_to": [u.get("name", {}).get("full") for u in (v.get("assignedUsers") or {}).get("nodes", [])],
        })
    return out


def get_client(client_id: str) -> Dict[str, Any]:
    return fetch_client_dashboard(client_id)
