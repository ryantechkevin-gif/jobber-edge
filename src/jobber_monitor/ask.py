"""
Natural-language Q&A over live Jobber data -- the "ask a question" feature
in the dashboard.

Deliberate design constraints:
  - Claude is only ever given a FIXED, small toolbelt of read-only Python
    functions below (from lookups.py, shared with monday_dashboard.py so
    both features compute things like "active recurring jobs" the same
    way). It can never run arbitrary GraphQL or call any Jobber mutation,
    no matter what's typed into the question box -- there's no code path
    from a question string to a write against the live account.
  - The tool results returned to the caller alongside the answer
    (`used_data`) are the literal JSON Claude was given, so an answer can
    always be checked against the underlying numbers rather than taken
    on faith.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

import anthropic

from . import lookups

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")


def _anthropic_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set as a Function App setting -- the ask "
            "feature needs it to call Claude."
        )
    return anthropic.Anthropic(api_key=api_key)


TOOLS = [
    {
        "name": "list_recurring_jobs",
        "description": (
            "List every recurring job across all clients, with client name/company, "
            "job title, status, monthly total, start/end dates, and whether autopay "
            "is enabled (autopay_enabled). Use for any question about recurring/"
            "subscription billing overall, e.g. total recurring monthly revenue, "
            "headcount of recurring clients, or who has autopay disabled. "
            "active_only=true (default) excludes only jobs whose status is "
            "'archived' -- job_status itself can be active/late/today/upcoming/"
            "action_required/on_hold/unscheduled/expiring_within_30_days/"
            "requires_invoicing, all of which are still ongoing recurring jobs."
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
    "list_recurring_jobs": lambda i: lookups.list_recurring_jobs(**i),
    "list_invoices": lambda i: lookups.list_invoices(**i),
    "search_clients_and_jobs": lambda i: lookups.search_clients_and_jobs(**i),
    "get_client": lambda i: lookups.get_client(**i),
    "list_clients": lambda i: lookups.list_clients(**i),
}

_MAX_TOOL_ROUNDS = 6
_MAX_RESULT_CHARS = 40_000  # guards the model's context, not a security boundary


def _system_prompt() -> str:
    today = datetime.now(lookups.BUSINESS_TZ).date().isoformat()
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
