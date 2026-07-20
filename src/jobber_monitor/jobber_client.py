"""
Jobber GraphQL API client (https://api.getjobber.com/api/graphql), verified
against Jobber's own developer docs rather than assumed:

  - Single endpoint, POST only, `application/json` body:
    {"query": ..., "variables": ...}
  - Auth: `Authorization: Bearer <access_token>`
  - Every request must carry `X-JOBBER-GRAPHQL-VERSION: YYYY-MM-DD` -- a
    dated schema version, not semver. Old versions stay supported for
    ~12 months after a newer one ships; Jobber returns a deprecation
    warning (under the response's `extensions`) once a pinned version is
    within 3 months of losing support. Pinned via JOBBER_API_VERSION so
    bumping it is a config change, not a code change.
  - Relay-style cursor pagination: `nodes { ... } pageInfo { hasNextPage
    endCursor }`, paged with `first`/`after`.

`execute()` is deliberately generic rather than one hardcoded function per
resource -- the goal is that anything in Jobber's schema is reachable
through this one function (see queries.py for what's been confirmed so
far, and the /api/jobber/schema introspection route for checking anything
that isn't yet).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .oauth import get_valid_access_token

API_BASE_URL = os.getenv("JOBBER_API_BASE_URL", "https://api.getjobber.com")
GRAPHQL_PATH = os.getenv("JOBBER_GRAPHQL_PATH", "/api/graphql")
API_VERSION = os.getenv("JOBBER_API_VERSION", "2025-04-16")

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is not None:
        return _session

    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=5)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    _session = s
    return s


class JobberGraphQLError(RuntimeError):
    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors
        super().__init__(f"Jobber GraphQL returned errors: {errors}")


def execute(query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = get_valid_access_token()
    sess = _get_session()

    r = sess.post(
        f"{API_BASE_URL}{GRAPHQL_PATH}",
        json={"query": query, "variables": variables or {}},
        headers={
            "Authorization": f"Bearer {token}",
            "X-JOBBER-GRAPHQL-VERSION": API_VERSION,
            "Content-Type": "application/json",
        },
        timeout=(5, 30),
    )
    if r.status_code >= 400:
        raise requests.HTTPError(
            f"HTTP {r.status_code} from Jobber GraphQL. Body: {r.text[:500]}", response=r
        )

    payload = r.json()

    versioning = (payload.get("extensions") or {}).get("versioning") or {}
    if versioning.get("warning"):
        print(f"JOBBER_API_VERSION_WARNING: {versioning['warning']}")

    if payload.get("errors"):
        raise JobberGraphQLError(payload["errors"])

    return payload.get("data") or {}


def paginate(
    query: str,
    connection_path: List[str],
    variables: Optional[Dict[str, Any]] = None,
    page_size: int = 50,
) -> List[Dict[str, Any]]:
    """
    Walks every page of a Relay-style connection. `query` must accept
    `$first: Int!` and `$after: String`; `connection_path` is the list of
    keys (within the `data` payload) leading to the {nodes, pageInfo}
    connection, e.g. ["clients"] or ["invoices"].
    """
    items: List[Dict[str, Any]] = []
    after: Optional[str] = None
    base_vars = dict(variables or {})

    while True:
        vars_ = dict(base_vars, first=page_size, after=after)
        data = execute(query, vars_)

        node = data
        for key in connection_path:
            node = node.get(key) or {}
        items.extend(node.get("nodes") or [])

        page_info = node.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break

    return items
