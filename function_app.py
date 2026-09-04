import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import azure.functions as func

from jobber_monitor import oauth
from jobber_monitor.ask import ask as ask_question
from jobber_monitor.jobber_client import execute
from jobber_monitor.monday_dashboard import build_monday_dashboard, save_this_weeks_snapshot
from jobber_monitor.notify_teams import build_monday_dashboard_message, post_teams_message
from jobber_monitor.queries import ACCOUNT_QUERY, INTROSPECT_TYPE_QUERY
from jobber_monitor.main import run as weekly_report_run
from jobber_monitor.report import fetch_client_dashboard

app = func.FunctionApp()

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "src", "jobber_monitor", "static")


# The actual browsable client dashboard -- a client picker (search-as-you-type
# over every client) plus the same per-client rollup as /api/jobber/client,
# rendered live in the browser. Requires the function key (?code=), which its
# own JS reuses for the /api/jobber/query calls it makes after loading.
@app.route(route="dashboard", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def dashboard_http(req: func.HttpRequest) -> func.HttpResponse:
    dashboard_path = os.path.join(_STATIC_DIR, "dashboard.html")
    with open(dashboard_path, "r", encoding="utf-8") as f:
        html = f.read()

    # File mtimes get reset to extraction time on every deploy (Azure
    # unzips the artifact fresh each time), so this doubles as a "last
    # deployed" stamp with zero manual bookkeeping -- shown in Arizona
    # time since that's where WeSpeakWiFi operates.
    deployed_at = datetime.fromtimestamp(os.path.getmtime(dashboard_path), tz=ZoneInfo("America/Phoenix"))
    build_stamp = deployed_at.strftime("Deployed %b %d, %Y · %-I:%M %p MST")
    html = html.replace("{{BUILD_STAMP}}", build_stamp)

    return func.HttpResponse(html, mimetype="text/html; charset=utf-8")


# The browsable Monday Dashboard -- a live-rendered view of
# build_monday_dashboard() fetched client-side from
# /api/jobber/monday-dashboard, same "code" query param reused for that
# call as dashboard_http does. This is the actual page people are meant
# to open Monday morning; the Teams message (posted by the timer below)
# is a summary, not a replacement for it. Accepts an optional
# ?week_end=YYYY-MM-DD to load a different week, same as the JSON route.
@app.route(route="monday-dashboard", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def monday_dashboard_http(req: func.HttpRequest) -> func.HttpResponse:
    page_path = os.path.join(_STATIC_DIR, "monday_dashboard.html")
    with open(page_path, "r", encoding="utf-8") as f:
        html = f.read()

    deployed_at = datetime.fromtimestamp(os.path.getmtime(page_path), tz=ZoneInfo("America/Phoenix"))
    build_stamp = deployed_at.strftime("Deployed %b %d, %Y · %-I:%M %p MST")
    html = html.replace("{{BUILD_STAMP}}", build_stamp)

    return func.HttpResponse(html, mimetype="text/html; charset=utf-8")


# One-time (or re-authorization) step: hit this with the function key to
# start Jobber's OAuth consent screen. Requires the function key since it
# kicks off a real authorization flow against the live WeSpeakWiFi Jobber
# account -- anyone who can trigger this can grant this app access.
@app.route(route="jobber/authorize", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def jobber_authorize(req: func.HttpRequest) -> func.HttpResponse:
    try:
        url = oauth.build_authorize_url()
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=500)
    return func.HttpResponse(status_code=302, headers={"Location": url})


# Jobber redirects the admin's own browser here after they approve access --
# it can't carry a function key, so this route has to be anonymous. CSRF
# risk is covered by the `state` value round-tripped through Jobber; see
# oauth.verify_state.
@app.route(route="jobber/callback", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def jobber_callback(req: func.HttpRequest) -> func.HttpResponse:
    error = req.params.get("error")
    if error:
        return func.HttpResponse(f"Jobber authorization was not granted: {error}", status_code=400)

    code = req.params.get("code")
    state = req.params.get("state")
    if not code:
        return func.HttpResponse("Missing ?code= from Jobber.", status_code=400)

    try:
        oauth.verify_state(state)
        oauth.exchange_code_for_token(code)
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=400)

    return func.HttpResponse(
        "WeSpeakWiFi's Jobber account is now connected. You can close this tab.",
        mimetype="text/plain; charset=utf-8",
    )


# Quick connectivity check once OAuth is connected -- confirms the access
# token (or a transparent refresh) actually works, without pulling the full
# weekly report.
@app.route(route="jobber/ping", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def jobber_ping(req: func.HttpRequest) -> func.HttpResponse:
    try:
        data = execute(ACCOUNT_QUERY)
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=400)
    return func.HttpResponse(str(data), mimetype="text/plain; charset=utf-8")


# Ad-hoc schema introspection: GET /api/jobber/schema?type=Job to see the
# real field names for a type before writing a query against it, instead of
# guessing -- especially for Job/Request/Visit, which aren't in queries.py yet.
@app.route(route="jobber/schema", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def jobber_schema(req: func.HttpRequest) -> func.HttpResponse:
    type_name = req.params.get("type")
    if not type_name:
        return func.HttpResponse("Missing required query param: type (e.g. ?type=Job)", status_code=400)
    try:
        data = execute(INTROSPECT_TYPE_QUERY, {"name": type_name})
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=400)
    return func.HttpResponse(json.dumps(data), mimetype="application/json; charset=utf-8")


# Ad-hoc GraphQL console: GET /api/jobber/query?q=<url-encoded query>
# [&vars=<url-encoded JSON>] runs ANY query or mutation against the live
# account and returns the raw result -- built for schema exploration during
# development (testing introspection variants like includeDeprecated,
# trying real data queries before committing them to queries.py) without a
# redeploy per question.
#
# SECURITY NOTE: this runs arbitrary mutations too, not just queries --
# equivalent in power to holding the OAuth token directly. Gated behind the
# function key like everything else here, but worth removing or locking
# down further once initial development against the live schema is done.
@app.route(route="jobber/query", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def jobber_query_http(req: func.HttpRequest) -> func.HttpResponse:
    query = req.params.get("q")
    if not query:
        return func.HttpResponse("Missing required query param: q (a GraphQL query/mutation string)", status_code=400)

    variables = None
    vars_raw = req.params.get("vars")
    if vars_raw:
        try:
            variables = json.loads(vars_raw)
        except ValueError:
            return func.HttpResponse(f"Invalid JSON in vars= param: {vars_raw!r}", status_code=400)

    try:
        data = execute(query, variables)
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=400)
    return func.HttpResponse(json.dumps(data), mimetype="application/json; charset=utf-8")


# Everything about one client in a single call -- identity, tags, notes,
# properties, jobs, quotes, invoices, requests. GET /api/jobber/client?id=<EncodedId>
# `id` is the client's Jobber id (e.g. copy it from a client's jobberWebUri,
# or from a clients() query result).
@app.route(route="jobber/client", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def jobber_client_http(req: func.HttpRequest) -> func.HttpResponse:
    client_id = req.params.get("id")
    if not client_id:
        return func.HttpResponse("Missing required query param: id (a client's EncodedId)", status_code=400)
    try:
        data = fetch_client_dashboard(client_id)
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=400)
    return func.HttpResponse(json.dumps(data), mimetype="application/json; charset=utf-8")


# The "ask a question" feature: GET /api/jobber/ask?q=<url-encoded question>
# hands the question to Claude along with a small fixed toolbelt of
# read-only Jobber lookups (see ask.py) -- it can never run arbitrary
# GraphQL or a mutation, only those specific safe queries, no matter what's
# typed in. Requires ANTHROPIC_API_KEY as a Function App setting.
@app.route(route="jobber/ask", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def jobber_ask_http(req: func.HttpRequest) -> func.HttpResponse:
    question = req.params.get("q")
    if not question:
        return func.HttpResponse("Missing required query param: q (a plain-English question)", status_code=400)
    try:
        result = ask_question(question)
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=400)
    return func.HttpResponse(json.dumps(result), mimetype="application/json; charset=utf-8")


# On-demand: build (and optionally post) the weekly report right now,
# without waiting for Monday's schedule -- safe to trigger repeatedly while
# testing since post=true is opt-in.
@app.route(route="jobber/report", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def jobber_report_http(req: func.HttpRequest) -> func.HttpResponse:
    do_post = req.params.get("post", "").strip().lower() in ("1", "true", "yes")
    try:
        text = weekly_report_run(post=do_post)
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=400)
    return func.HttpResponse(text, mimetype="text/plain; charset=utf-8")


# The live-API Monday Dashboard (monday_dashboard.py) -- the replacement
# for the manual 5-CSV weekly process, now wired into the Monday timer
# below after backtesting against the Aug 17-23 week matched or explained
# every section except Monthly Recurring (a ~$931/4-job gap still being
# chased down separately -- see the backtest reconciliation report).
# GET for a quick check (no WeeFee data, since GET has no body); POST
# with a JSON body to attach that week's WeeFee export and/or backtest a
# specific past week:
#   GET  /api/jobber/monday-dashboard?week_end=2026-08-23&post=false
#   POST /api/jobber/monday-dashboard  {"week_end": "2026-08-23", "weefee": {...}, "post": false}
# post=true also posts the Teams summary and saves this week's snapshot
# for next week's Week-over-Week comparison -- off by default so this can
# be hit repeatedly while testing/backtesting without side effects.
@app.route(route="jobber/monday-dashboard", methods=["GET", "POST"], auth_level=func.AuthLevel.FUNCTION)
def jobber_monday_dashboard_http(req: func.HttpRequest) -> func.HttpResponse:
    weefee = None
    week_end_raw = req.params.get("week_end")
    do_post = req.params.get("post", "").strip().lower() in ("1", "true", "yes")

    if req.method == "POST":
        try:
            body = req.get_json()
        except ValueError:
            body = {}
        week_end_raw = body.get("week_end", week_end_raw)
        weefee = body.get("weefee")
        if "post" in body:
            do_post = bool(body["post"])

    week_end = None
    if week_end_raw:
        try:
            week_end = date.fromisoformat(week_end_raw)
        except ValueError:
            return func.HttpResponse(f"Invalid week_end (expected YYYY-MM-DD): {week_end_raw!r}", status_code=400)

    try:
        dashboard = build_monday_dashboard(week_end=week_end, weefee=weefee)
        if do_post:
            webhook = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
            if webhook:
                post_teams_message(webhook, build_monday_dashboard_message(dashboard))
            save_this_weeks_snapshot(dashboard)
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=400)

    return func.HttpResponse(json.dumps(dashboard, default=str), mimetype="application/json; charset=utf-8")


# Monday 8am America/Phoenix (UTC-7 year-round, no DST) = 15:00 UTC --
# replaces the manual 5-CSV weekly process that used to run off Jobber's
# own report-scheduler emails. WeeFee isn't wired in here yet (still no
# confirmed export source for it -- see normalize_weefee's default), so
# this runs without it until that's resolved.
@app.timer_trigger(schedule="0 0 15 * * 1", arg_name="mytimer", run_on_startup=False, use_monitor=True)
def weekly_report_timer(mytimer: func.TimerRequest) -> None:
    dashboard = build_monday_dashboard()
    webhook = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
    if webhook:
        post_teams_message(webhook, build_monday_dashboard_message(dashboard))
    save_this_weeks_snapshot(dashboard)
