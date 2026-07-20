"""
Weekly Jobber report -- the live-API replacement for the CSV-based Monday
Dashboard (previously built by hand from the report-scheduler@apps.getjobber.com
CSV export emails).

Starts with what's confirmed against Jobber's schema (clients, invoices,
quotes). Status breakdowns are grouped by whatever value the live API
actually returns rather than assuming specific enum strings (e.g. which
exact status means "paid" vs "overdue") -- safer than guessing, and still
gives a real, readable picture of where things stand. Jobs/requests/visits
sections are intentionally left out until their field names are confirmed
against the live schema (see queries.py) rather than added with guessed
field names that might silently break or lie.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from .jobber_client import paginate
from .queries import CLIENTS_QUERY, INVOICES_QUERY, QUOTES_QUERY


def _breakdown(items: List[Dict[str, Any]], status_key: str, amount_key: Optional[str] = None) -> Dict[str, Any]:
    counts: Counter = Counter()
    totals: Counter = Counter()
    for item in items:
        status = item.get(status_key) or "UNKNOWN"
        counts[status] += 1
        if amount_key:
            totals[status] += float(item.get(amount_key) or 0)
    return {"counts": dict(counts), "totals": dict(totals) if amount_key else {}}


def build_weekly_report() -> Dict[str, Any]:
    clients = paginate(CLIENTS_QUERY, ["clients"])
    invoices = paginate(INVOICES_QUERY, ["invoices"])
    quotes = paginate(QUOTES_QUERY, ["quotes"])

    active_clients = [c for c in clients if not c.get("isArchived")]

    return {
        "client_count": len(clients),
        "active_client_count": len(active_clients),
        "invoice_count": len(invoices),
        "invoice_breakdown": _breakdown(invoices, "invoiceStatus", "total"),
        "quote_count": len(quotes),
        "quote_breakdown": _breakdown(quotes, "quoteStatus"),
    }
