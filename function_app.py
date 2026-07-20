import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import azure.functions as func

from jobber_monitor import oauth
from jobber_monitor.jobber_client import execute
from jobber_monitor.queries import ACCOUNT_QUERY, INTROSPECT_TYPE_QUERY
from jobber_monitor.main import run as weekly_report_run

app = func.FunctionApp()


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
    return func.HttpResponse(str(data), mimetype="application/json; charset=utf-8")


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
    return func.HttpResponse(str(data), mimetype="application/json; charset=utf-8")


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


# Monday 8am America/Phoenix (UTC-7 year-round, no DST) = 15:00 UTC --
# replaces the CSV-based Monday Dashboard that used to run off Jobber's own
# report-scheduler emails.
@app.timer_trigger(schedule="0 0 15 * * 1", arg_name="mytimer", run_on_startup=False, use_monitor=True)
def weekly_report_timer(mytimer: func.TimerRequest) -> None:
    weekly_report_run(post=True)
