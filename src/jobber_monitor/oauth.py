"""
Jobber OAuth 2.0 -- authorization code grant with rotating refresh tokens.
Verified against Jobber's own developer docs (developer.getjobber.com/docs/
building_your_app/app_authorization/ and .../refresh_token_rotation/), not
assumed:

  - Authorize: GET https://api.getjobber.com/api/oauth/authorize
      ?response_type=code&client_id=...&redirect_uri=...&state=...
    `redirect_uri` must exactly match what's registered for the app in
    Jobber's Developer Center. `state` is round-tripped back on the
    callback -- Jobber's own docs call this out as CSRF protection.

  - Token exchange / refresh: POST https://api.getjobber.com/api/oauth/token
    (form-encoded body). Authorization code grant needs client_id,
    client_secret, grant_type=authorization_code, code, redirect_uri.
    Refresh needs client_id, client_secret, grant_type=refresh_token,
    refresh_token.

  - Access tokens expire in 60 minutes. Refresh Token Rotation hands back
    a *new* refresh_token on every refresh -- the old one is invalidated
    immediately, and reusing it fails outright ("The provided refresh
    token is not valid"). The rotated value must be persisted every time,
    not just the access_token, or the whole connection goes dead the next
    time a refresh is attempted with the stale one.
"""
from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

from .state_store import load_json, save_json, delete_blob

TOKEN_BLOB = "oauth_token.json"
PENDING_STATE_BLOB = "oauth_pending_state.json"
STATE_TTL_SECONDS = 600  # 10 minutes to complete the Jobber consent screen

API_BASE_URL = os.getenv("JOBBER_API_BASE_URL", "https://api.getjobber.com")
AUTHORIZE_PATH = os.getenv("JOBBER_OAUTH_AUTHORIZE_PATH", "/api/oauth/authorize")
TOKEN_PATH = os.getenv("JOBBER_OAUTH_TOKEN_PATH", "/api/oauth/token")

# A refreshed access token is swapped out this long before its real expiry
# (Jobber: 60 min) so a request never starts against a token that expires
# mid-flight.
EXPIRY_BUFFER_SECONDS = 120


def _client_id() -> str:
    v = os.getenv("JOBBER_CLIENT_ID", "").strip()
    if not v:
        raise RuntimeError("Missing JOBBER_CLIENT_ID.")
    return v


def _client_secret() -> str:
    v = os.getenv("JOBBER_CLIENT_SECRET", "").strip()
    if not v:
        raise RuntimeError("Missing JOBBER_CLIENT_SECRET.")
    return v


def _redirect_uri() -> str:
    v = os.getenv("JOBBER_REDIRECT_URI", "").strip()
    if not v:
        raise RuntimeError(
            "Missing JOBBER_REDIRECT_URI -- must exactly match the redirect URI "
            "registered for this app in Jobber's Developer Center."
        )
    return v


def build_authorize_url() -> str:
    """
    Generates a fresh CSRF `state` value, stashes it (short TTL) so the
    callback can verify it, and returns the URL to send the browser to.
    """
    state = secrets.token_urlsafe(24)
    save_json(PENDING_STATE_BLOB, {"state": state, "created_at": time.time()})

    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "state": state,
    }
    return f"{API_BASE_URL}{AUTHORIZE_PATH}?{urlencode(params)}"


def verify_state(state: Optional[str]) -> None:
    pending = load_json(PENDING_STATE_BLOB)
    if not pending:
        raise RuntimeError("No pending authorization in progress -- start at /api/jobber/authorize.")
    if time.time() - pending.get("created_at", 0) > STATE_TTL_SECONDS:
        raise RuntimeError("Authorization attempt expired -- start again at /api/jobber/authorize.")
    if not state or state != pending.get("state"):
        raise RuntimeError("State mismatch on Jobber callback -- possible CSRF, or a stale/duplicate request.")
    delete_blob(PENDING_STATE_BLOB)


def _save_token_response(payload: Dict[str, Any]) -> None:
    save_json(
        TOKEN_BLOB,
        {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "obtained_at": datetime.now(timezone.utc).isoformat(),
            "expires_in": payload.get("expires_in", 3600),
        },
    )


def exchange_code_for_token(code: str) -> None:
    r = requests.post(
        f"{API_BASE_URL}{TOKEN_PATH}",
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise requests.HTTPError(
            f"Jobber token exchange failed: HTTP {r.status_code}: {r.text[:500]}", response=r
        )
    _save_token_response(r.json())


def _refresh(refresh_token: str) -> Dict[str, Any]:
    r = requests.post(
        f"{API_BASE_URL}{TOKEN_PATH}",
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise requests.HTTPError(
            f"Jobber token refresh failed: HTTP {r.status_code}: {r.text[:500]}", response=r
        )
    return r.json()


def get_valid_access_token() -> str:
    """
    Returns a live access token, transparently refreshing (and re-persisting
    the rotated refresh_token) if the current one is expired or about to be.
    Raises RuntimeError if nobody has completed the one-time
    /api/jobber/authorize consent step yet.
    """
    token = load_json(TOKEN_BLOB)
    if not token:
        raise RuntimeError(
            "No Jobber authorization on file -- visit /api/jobber/authorize once "
            "(with the function key) to connect this app to the WeSpeakWiFi Jobber account."
        )

    obtained_at = datetime.fromisoformat(token["obtained_at"])
    expires_at = obtained_at + timedelta(seconds=token.get("expires_in", 3600))
    if datetime.now(timezone.utc) < expires_at - timedelta(seconds=EXPIRY_BUFFER_SECONDS):
        return token["access_token"]

    fresh = _refresh(token["refresh_token"])
    _save_token_response(fresh)
    return fresh["access_token"]
