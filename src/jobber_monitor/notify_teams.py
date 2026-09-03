from __future__ import annotations

from datetime import date
from typing import Any, Dict

import requests


def post_teams_message(webhook_url: str, text: str) -> None:
    # Same Power Automate flow used by starlink-edge/unifi-edge -- expects a
    # {"message": ...} body, not the classic Incoming Webhook {"text": ...} shape.
    payload = {"message": text}
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()


def build_weekly_report_message(report: Dict[str, Any]) -> str:
    lines = ["**WeSpeakWiFi Weekly Jobber Report** (live via Jobber API)"]

    lines.append(f"- Clients: {report['client_count']} total, {report['active_client_count']} active")

    lines.append(f"- Invoices: {report['invoice_count']} total")
    breakdown = report["invoice_breakdown"]
    for status, count in sorted(breakdown["counts"].items()):
        total = breakdown["totals"].get(status, 0)
        lines.append(f"  - {status}: {count} (${total:,.2f})")

    lines.append(f"- Quotes: {report['quote_count']} total")
    for status, count in sorted(report["quote_breakdown"]["counts"].items()):
        lines.append(f"  - {status}: {count}")

    lines.append("")
    lines.append("_-- Kook \U0001F916 (Claude-powered, not Kevin)_")

    return "\n".join(lines)


def _fmt_short_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%b %-d")


def build_monday_dashboard_message(dashboard: Dict[str, Any]) -> str:
    """
    Teams summary for the live-API Monday Dashboard (monday_dashboard.py)
    -- this replaces build_weekly_report_message above for the Monday
    send once wired up; that function stays for the on-demand
    /api/jobber/report route.
    """
    week_range = f"{_fmt_short_date(dashboard['week_start'])}–{_fmt_short_date(dashboard['week_end'])}, {date.fromisoformat(dashboard['week_end']).year}"
    lines = [f"**WSW Monday Dashboard** — Week of {week_range}"]

    mr = dashboard["monthly_recurring"]
    lines.append(f"- Active Recurring: {mr['job_count']} jobs / ${mr['mrr_total']:,.2f} mo — {mr['client_count']} clients")

    br = dashboard["billing_risk"]
    ghost_note = ""
    if br["ghost_balance_invoice_count"]:
        ghost_note = f" (includes ${br['ghost_balance_total']:,.2f} across {br['ghost_balance_invoice_count']} invoices marked paid but not zeroed out -- a data quirk, not new past-due activity)"
    lines.append(f"- Clients with Balance: {br['clients_with_balance_count']} clients / ${br['total_outstanding']:,.2f} outstanding{ghost_note}")

    lines.append(f"- Incomplete Jobs: {dashboard['incomplete_jobs']['count']} open")

    ptw = dashboard["paid_this_week"]
    lines.append(f"- Paid This Week: {ptw['count']} invoices / ${ptw['collected_total']:,.2f} collected")

    lines.append(f"- Visits This Week: {dashboard['visits_this_week']['count']}")

    wow = dashboard["week_over_week"]
    if not wow["has_previous"]:
        lines.append("- Week-over-Week: no prior snapshot to compare against yet")
    else:
        new_n = len(wow["new_recurring_jobs"])
        closed_n = len(wow["closed_recurring_jobs"])
        if new_n == 0 and closed_n == 0 and wow["mrr_change"] == 0 and wow["client_count_change"] == 0:
            lines.append(
                f"- Week-over-Week: no change — same {mr['job_count']} active recurring jobs, "
                f"same ${mr['mrr_total']:,.2f} MRR, same {mr['client_count']} clients as last week"
            )
        else:
            lines.append(
                f"- Week-over-Week: {new_n} new / {closed_n} closed recurring jobs, "
                f"MRR change ${wow['mrr_change']:+,.2f}, client count change {wow['client_count_change']:+d}"
            )

    wf = dashboard["weefee"]
    if wf["provided"]:
        lines.append(
            f"- WeeFee (AI Receptionist): {wf['total_conversations']} conversations "
            f"({wf['calls']} calls / {wf['texts']} texts / {wf['web_chats']} web chats), "
            f"{wf['needs_attention']} needing attention"
        )
    else:
        lines.append("- WeeFee (AI Receptionist): not provided this week")

    if dashboard["priority_actions"]["count"]:
        lines.append(f"- Priority Actions: {dashboard['priority_actions']['count']} flagged (see dashboard for detail)")

    lines.append("")
    lines.append("_-- Kook \U0001F916 (Claude-powered, not Kevin)_")

    return "\n".join(lines)
