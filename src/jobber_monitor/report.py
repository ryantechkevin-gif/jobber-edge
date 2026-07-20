"""
Weekly Jobber report -- the live-API replacement for the CSV-based Monday
Dashboard (previously built by hand from the report-scheduler@apps.getjobber.com
CSV export emails).

Covers clients, invoices, and quotes for now. Status breakdowns are
grouped by whatever value the live API actually returns rather than
assuming specific enum strings (e.g. which exact status means "paid" vs
"overdue") -- safer than guessing, and still gives a real, readable
picture of where things stand.

fetch_client_dashboard() is the other half of this module: a full
single-client rollup (jobs/quotes/invoices/requests/notes/properties),
built from CLIENT_DASHBOARD_QUERY in queries.py.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from .jobber_client import execute, paginate
from .queries import CLIENT_DASHBOARD_QUERY, CLIENTS_QUERY, INVOICES_QUERY, QUOTES_QUERY


def fetch_client_dashboard(client_id: str) -> Dict[str, Any]:
    data = execute(CLIENT_DASHBOARD_QUERY, {"id": client_id})
    return data.get("client") or {}


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

    # `total` lives under `amounts` on both Invoice and Quote, not as a
    # flat field (confirmed via schema introspection) -- flattened here so
    # _breakdown's simple dict.get(amount_key) still works.
    for invoice in invoices:
        invoice["total"] = (invoice.get("amounts") or {}).get("total")
    for quote in quotes:
        quote["total"] = (quote.get("amounts") or {}).get("total")

    return {
        "client_count": len(clients),
        "active_client_count": len(active_clients),
        "invoice_count": len(invoices),
        "invoice_breakdown": _breakdown(invoices, "invoiceStatus", "total"),
        "quote_count": len(quotes),
        "quote_breakdown": _breakdown(quotes, "quoteStatus"),
    }
