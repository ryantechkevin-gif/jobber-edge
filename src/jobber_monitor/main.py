from __future__ import annotations

import os

from dotenv import load_dotenv

from .report import build_weekly_report
from .notify_teams import post_teams_message, build_weekly_report_message


def run(post: bool = True) -> str:
    load_dotenv()
    webhook = os.getenv("TEAMS_WEBHOOK_URL", "").strip()

    report = build_weekly_report()
    text = build_weekly_report_message(report)

    if post and webhook:
        post_teams_message(webhook, text)

    return text


if __name__ == "__main__":
    print(run(post=False))
