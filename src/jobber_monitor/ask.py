"""
Natural-language Q&A over live Jobber data -- the "ask a question" feature
in the dashboard.

Deliberate design constraints:
  - Claude is only ever given a FIXED, small toolbelt of read-only Python
    functions below. It can never run arbitrary GraphQL or call any
    Jobber mutation, no matter what's typed into the question box --
    there's no code path from a question string to a write against the
    live account.
  - Every tool does its own filtering/sorting/aggregation in Python
    against data pulled through the same execute()/paginate() helpers
    used elsewhere in this app, rather than trusting Jobber's own
    (limited, inconsistently available) filter/sort arguments -- e.g.
    there's no confirmed root-level `jobs` connection, and Invoice/Quote
    nest `total` under `amounts` rather than as a flat field. Claude
    reasons over real, already-correct numbers instead of writing its own
    GraphQL against a schema it doesn't fully know.
  - The tool results returned to the caller alongside the answer
    (`used_data`) are the literal JSON Claude was given, so an answer can
    always be checked against the underlying numbers rather than taken
    on faith.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import anthropic

from .jobber_client import paginate
from .queries import CLIENTS_FULL_QUERY, CLIENTS_QUERY, INVOICES_QUERY
from .report import fetch_client_dashboard

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
_BUSINESS_TZ = ZoneInfo("America/Phoenix")


def _anthropic_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set as a Function App setting -- the ask "
            "feature needs it to call Claude."
        )
    return anthropic.Anthropic(api_key=api_key)


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# Cached for a short window so a single question that triggers several
# tool calls (e.g. list_recurring_jobs then search_clients_and_jobs)
# doesn't re-fetch every client from Jobber each time -- correctness
# doesn't depend on this (recurring billing data doesn't change
# second-to-second), it's purely to keep one question's Jobber round
# trips down.
_cache: Dict[str, Any] = {"clients_full": None, "clients_full_at": 0.0}
_CACHE_TTL_SECONDS = 60


def _all_clients_full() -> List[Dict[str, Any]]:
    now = time.time()
    if _cache["clients_full"] is not None and (now - _cache["clients_full_at"]) < _CACHE_TTL_SECONDS:
        return _cache["clients_full"]
    clients = paginate(CLIENTS_FULL_QUERY, ["clients"])
    _cache["clients_full"] = clients
    _cache["clients_full_at"] = now
    return clients


def _custom_field_value(field: Dict[str, Any]) -> Any:
    return field.get("valueText") if field.get("valueText") is not None else field.get("valueDropdown")


def _property_summaries(client: Dict[str, Any]) -> List[Dict[str, Any]]:
    props = []
    for p in (client.get("clientProperties") or {}).get("nodes", []):
        addr = p.get("address") or {}
        fields = {
            f.get("label"): _custom_field_value(f)
            for f in (p.get("customFields") or [])
            if _custom_field_value(f) not in (None, "")
        }
        props.append({
            "property_id": p.get("id"),
            "name": p.get("name"),
            "address": ", ".join(filter(None, [addr.get("street"), addr.get("city"), addr.get("province"), addr.get("postalCode")])),
            "custom_fields": fields,
        })
    return props


def _job_summaries(client: Dict[str, Any], recurring_only: bool, active_only: bool) -> List[Dict[str, Any]]:
    out = []
    for j in (client.get("jobs") or {}).get("nodes", []):
        if recurring_only and j.get("jobType") != "RECURRING":
            continue
        if active_only and (j.get("jobStatus") or "").upper() != "ACTIVE":
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


# ---------- tools ----------

def _list_recurring_jobs(active_only: bool = True) -> List[Dict[str, Any]]:
    out = []
    for c in _all_clients_full():
        jobs = _job_summaries(c, recurring_only=True, active_only=active_only)
        for j in jobs:
            out.append({
                "client_id": c["id"],
                "client_name": c.get("name"),
                "company_name": c.get("companyName"),
                "client_archived": c.get("isArchived"),
                **j,
            })
    return out


def _list_invoices(
    since_date: Optional[str] = None,
    until_date: Optional[str] = None,
    recurring_only: bool = False,
) -> List[Dict[str, Any]]:
    invoices = paginate(INVOICES_QUERY, ["invoices"])
    since = _parse_date(since_date)
    until = _parse_date(until_date) or datetime.now(_BUSINESS_TZ).date()

    out = []
    for inv in invoices:
        when = _parse_date(inv.get("issuedDate")) or _parse_date(inv.get("createdAt"))
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
            "jobber_web_uri": inv.get("jobberWebUri"),
        })
    out.sort(key=lambda i: i["issued_date"] or "", reverse=True)
    return out


def _search_clients_and_jobs(term: str) -> List[Dict[str, Any]]:
    needle = term.strip().lower()
    matches = []
    for c in _all_clients_full():
        props = _property_summaries(c)
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
            "jobs": _job_summaries(c, recurring_only=False, active_only=False),
        })
    return matches


def _list_clients(active_only: bool = False) -> List[Dict[str, Any]]:
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


def _get_client(client_id: str) -> Dict[str, Any]:
    return fetch_client_dashboard(client_id)


TOOLS = [
    {
        "name": "list_recurring_jobs",
        "description": (
            "List every recurring job across all clients, with client name/company, "
            "job title, status, monthly total, and start/end dates. Use for any "
            "question about recurring/subscription billing overall, e.g. total "
            "recurring monthly revenue or headcount of recurring clients."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "active_only": {
                    "type": "boolean",
                    "description": "If true (default), only ACTIVE recurring jobs -- excludes completed/cancelled ones.",
                }
            },
        },
    },
    {
        "name": "list_invoices",
        "description": (
            "List invoices issued within a date range (inclusive), newest first, "
            "with invoice number, status, issued/due dates, subject, total "
            "amount, the billed client's name, and whether the invoice bills a "
            "recurring job (is_recurring, based on the jobs actually linked to "
            "the invoice). Use for billing-history questions over a time window; "
            "set recurring_only=true for questions specifically about recurring/"
            "subscription invoices."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since_date": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive lower bound. Omit for none."},
                "until_date": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive upper bound. Omit to default to today."},
                "recurring_only": {"type": "boolean", "description": "Only include invoices that bill at least one recurring job. Default false."},
            },
        },
    },
    {
        "name": "search_clients_and_jobs",
        "description": (
            "Search clients by free text matched against name, company name, "
            "property name/address, property custom field labels/values (SSID/"
            "network info), AND every job title. Use when a question names a "
            "specific service/ISP/product/keyword. Important: WeSpeakWiFi's own "
            "recurring billing line items are usually generic ('WeSpeakWiFi "
            "Monthly 1 Gig Service') regardless of the underlying ISP -- the "
            "ISP itself, when identifiable at all, mostly shows up as shorthand "
            "in one-off JOB TITLES (e.g. 'Quantum Install', 'Quantum Switch', "
            "'Cox Install', 'Google Fiber Install', 'Starlink Residential'), "
            "not as a clean tag. If a full phrase like 'Quantum Fiber' returns "
            "nothing, retry with just the brand word ('Quantum') before "
            "concluding there are no matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"term": {"type": "string", "description": "Search term, e.g. 'Quantum' (prefer the short brand word over a full phrase)."}},
            "required": ["term"],
        },
    },
    {
        "name": "get_client",
        "description": (
            "Fetch one client's full record by Jobber client id -- identity, "
            "notes, properties/custom fields, jobs, quotes, invoices, requests. "
            "Use to pull full detail on a client already identified by another tool call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"client_id": {"type": "string", "description": "The client's Jobber EncodedId."}},
            "required": ["client_id"],
        },
    },
    {
        "name": "list_clients",
        "description": (
            "List every client (active and archived, unless active_only) with "
            "id, name, company, archived flag, created date, emails, phones. "
            "Use for headcount/tenure questions not tied to jobs or invoices."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"active_only": {"type": "boolean", "description": "Exclude archived clients. Default false."}},
        },
    },
]

_DISPATCH = {
    "list_recurring_jobs": lambda i: _list_recurring_jobs(**i),
    "list_invoices": lambda i: _list_invoices(**i),
    "search_clients_and_jobs": lambda i: _search_clients_and_jobs(**i),
    "get_client": lambda i: _get_client(**i),
    "list_clients": lambda i: _list_clients(**i),
}

_MAX_TOOL_ROUNDS = 6
_MAX_RESULT_CHARS = 40_000  # guards the model's context, not a security boundary


def _system_prompt() -> str:
    today = datetime.now(_BUSINESS_TZ).date().isoformat()
    return (
        "You are Kook -- that's the nickname WeSpeakWiFi's owner uses for the "
        "Claude-powered assistant that helps run this business, and staff "
        "already know 'Kook' means an AI, not the owner. If asked who you are "
        "or whether you're Claude, say you're Kook, built on Claude/Anthropic -- "
        "be upfront that you're an AI assistant, never imply you're a human "
        "staff member. You're embedded in WeSpeakWiFi's internal Jobber "
        "dashboard, answering the owner's plain-English questions about their "
        f"real client/job/invoice data. Today's date is {today} (America/Phoenix -- "
        "WeSpeakWiFi's own timezone; use this for any 'last N days'/'this month' "
        "math). Use the tools to gather real data before answering -- never "
        "guess or estimate a number. Cite concrete counts and dollar totals "
        "computed from the tool results, list specific clients/invoices by name "
        "when asked to 'list them', and format money as USD with two decimals. "
        "If a question is ambiguous (e.g. which ISP/service a term refers to), "
        "make your best reasonable interpretation and say so briefly rather than "
        "asking a clarifying question back, since this is a one-shot Q&A box. "
        "For questions about which clients are billed monthly for a specific ISP "
        "(e.g. Quantum Fiber, Cox, Starlink, Google Fiber): a client's recurring "
        "billing job is often titled generically ('WeSpeakWiFi Monthly 1 Gig "
        "Service') even when the ISP is known from an earlier one-off job on "
        "the same client (e.g. 'Quantum Install', 'Quantum Switch'). So: search "
        "for the ISP by name to find which clients are associated with it at "
        "all, then look at THAT client's own recurring job(s) (jobType "
        "RECURRING, regardless of that job's own title) for the actual monthly "
        "amount and dates -- don't require the recurring job itself to mention "
        "the ISP by name."
    )


def ask(question: str) -> Dict[str, Any]:
    client = _anthropic_client()
    messages: List[Dict[str, Any]] = [{"role": "user", "content": question}]
    used_data: List[Dict[str, Any]] = []

    for _ in range(_MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=_system_prompt(),
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text")
            return {"answer": text, "used_data": used_data}

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                result = _DISPATCH[block.name](block.input or {})
                result_json = json.dumps(result, default=str)
            except Exception as exc:  # noqa: BLE001 -- surfaced to Claude as a tool error, not raised
                result_json = json.dumps({"error": str(exc)})

            used_data.append({"tool": block.name, "input": block.input, "result": json.loads(result_json)})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_json[:_MAX_RESULT_CHARS],
            })
        messages.append({"role": "user", "content": tool_results})

    return {
        "answer": "I wasn't able to finish answering within the tool-call budget for one question -- try breaking it into something narrower.",
        "used_data": used_data,
    }
