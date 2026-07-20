from __future__ import annotations

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

    return "\n".join(lines)
