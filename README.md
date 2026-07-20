# jobber-edge

Azure Function App integrating WeSpeakWiFi's Jobber account via Jobber's
GraphQL API. First goal: replace the weekly Monday Dashboard, which today
is built by hand from CSV export emails Jobber's own report-scheduler
sends (`report-scheduler@apps.getjobber.com`), with a report pulled live
from the API. Longer term, the intent is that any data available through
Jobber's API should be reachable through this integration, not just what
the CSV exports happened to cover.

Sibling to `unifi-edge`, `starlink-edge`, and `eero-watchdog` -- same
overall shape (Azure Function App, Teams webhook, Blob Storage for state)
but Jobber is its own, unrelated data source, hence its own repo.

## How it's authenticated

Unlike UniFi's static `X-Api-Key`, Jobber's API uses OAuth 2.0
(authorization code grant, with **rotating** refresh tokens) -- verified
against Jobber's own developer docs
([App Authorization](https://developer.getjobber.com/docs/building_your_app/app_authorization/),
[Refresh Token Rotation](https://developer.getjobber.com/docs/building_your_app/refresh_token_rotation/)):

| Setting | Purpose |
|---|---|
| `JOBBER_CLIENT_ID` / `JOBBER_CLIENT_SECRET` | From the app registered in Jobber's [Developer Center](https://developer.getjobber.com/) |
| `JOBBER_REDIRECT_URI` | Must exactly match the redirect URI registered for that app |
| `JOBBER_API_BASE_URL` | `https://api.getjobber.com` |
| `JOBBER_OAUTH_AUTHORIZE_PATH` | `/api/oauth/authorize` |
| `JOBBER_OAUTH_TOKEN_PATH` | `/api/oauth/token` |
| `JOBBER_GRAPHQL_PATH` | `/api/graphql` |
| `JOBBER_API_VERSION` | Dated schema version (e.g. `2025-04-16`), sent as `X-JOBBER-GRAPHQL-VERSION` on every request. Jobber supports a version for ~12 months after a newer one ships and returns a deprecation warning (logged to the function's console) once a pinned version is within 3 months of aging out -- bump this setting when that happens. |

**One-time setup:** after deploying, visit `/api/jobber/authorize` (with the
function key) in a browser. That sends you to Jobber's consent screen;
approving it redirects to `/api/jobber/callback`, which exchanges the
authorization code for an access + refresh token and stores them in Blob
Storage (`oauth_token.json`, container `jobber-monitor-state` by default).
From then on, every run transparently refreshes the access token as
needed -- **and re-persists the rotated refresh token every time**, since
Jobber invalidates the old one the instant a new one is issued. If that
ever gets out of sync (e.g. state restored from an old backup), the fix
is just re-running `/api/jobber/authorize`.

This flow only works against a real, publicly reachable callback URL --
it can't be completed against `localhost`.

**Target Function App: `jobberwsw`.** That app already exists from an
earlier, abandoned Power BI integration attempt -- this repo replaces its
code entirely (see Deploying below) rather than standing up a new app, so
`JOBBER_REDIRECT_URI` stays exactly what's already registered in Jobber's
Developer Center:
`https://jobberwsw-d2d4e3dmafaydbbu.westus-01.azurewebsites.net/api/jobber/callback`

**Security note:** tokens are stored as plain JSON in the same Blob
Storage account the Function App already uses for its own bookkeeping
(`AzureWebJobsStorage`) -- same trust boundary as that storage account's
access key, not a dedicated secrets store like Key Vault. Consistent with
how the sibling repos store their own state, but worth hardening to Key
Vault later given these tokens grant full account access.

## What's confirmed vs. still needs checking

Verified against real Jobber integration references and cross-checked
against Jobber's docs: the `account`, `clients`, `invoices`, and `quotes`
queries in `src/jobber_monitor/queries.py`.

**Not yet confirmed:** field names for jobs, requests, visits, expenses,
or the "client communications" dataset that WeSpeakWiFi's existing Client
Communications Audit report already pulls from Jobber somehow. Rather
than guess and risk a query that's subtly wrong, use the introspection
route below once OAuth is connected:

```
GET /api/jobber/schema?type=Job
GET /api/jobber/schema?type=Request
GET /api/jobber/schema?type=Visit
```

This returns the real field names/types for anything in Jobber's schema,
so the weekly report (and whatever gets built after it) can be extended
against confirmed fields instead of assumptions.

## Weekly report

Runs Monday 8am America/Phoenix (15:00 UTC, no DST there) and posts to
`TEAMS_WEBHOOK_URL` (same `{"message": ...}` Power Automate flow as the
sibling repos). Currently covers client counts and invoice/quote status
breakdowns -- grouped by whatever status value the live API actually
returns, rather than assuming specific enum strings up front.

Trigger it on demand instead of waiting for Monday:

```
GET /api/jobber/report                # returns the report text only
GET /api/jobber/report?post=true      # also posts to Teams
```

(Requires the function key -- not anonymous.)

## Local setup

```
cp local.settings.json.example local.settings.json
# fill in local.settings.json with real values (it's gitignored)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start
```

Note: the OAuth consent step itself has to run against a deployed, public
callback URL -- not `localhost` -- so a local run can exercise the GraphQL
client and report logic (once a token already exists in Blob Storage) but
can't complete the initial authorization on its own.

## Deploying

Not wired to Azure yet. This repo replaces the code currently running in
the existing `jobberwsw` Function App (the abandoned Power BI attempt) --
same overall steps as `unifi-edge`/`starlink-edge`:

1. In `jobberwsw`'s **Deployment Center**, connect this GitHub repo,
   branch `main`. Azure Portal generates the matching
   `.github/workflows/*.yml` with the correct app name and
   `AZUREAPPSERVICE_*` secrets wired up -- the next push overwrites
   whatever's currently deployed there.
2. In its **Configuration**, clear out any leftover settings from the old
   Power BI integration, and set the App Settings listed above
   (`JOBBER_CLIENT_ID`, `JOBBER_CLIENT_SECRET`, `JOBBER_REDIRECT_URI`,
   `TEAMS_WEBHOOK_URL`, `PYTHONPATH=src`, etc). `AzureWebJobsStorage`
   should already be set -- Azure provisions that automatically.
3. Confirm `JOBBER_REDIRECT_URI` matches the redirect URI already
   registered in Jobber's Developer Center (it should, unchanged), then
   run the one-time
   `/api/jobber/authorize` step above.
