# Hosting Retrieval Studio

The app runs in one of two **deploy modes**, chosen by the `RETRIEVAL_MODE`
environment variable.

## `local` (default)
Single user, persistent studies under `data/studies/`. This is the desktop
experience — nothing about it changes. No env vars needed:

```bash
python -m streamlit run app.py
```

## `hosted`
For putting the app in front of other people (each brings their own OpenAI
key). Every browser session gets an **isolated, temporary workspace** under
`data/sessions/{session_id}/`; visitors can never see each other's studies,
and workspaces are garbage-collected after a TTL and on explicit "Clear /
Delete my data".

```bash
export RETRIEVAL_MODE=hosted
export RETRIEVAL_ACCESS_CODE=your-shared-code   # optional gate (recommended)
python -m streamlit run app.py
```

| Env var | Effect |
|---|---|
| `RETRIEVAL_MODE` | `local` (default) or `hosted` |
| `RETRIEVAL_ACCESS_CODE` | If set in hosted mode, visitors must enter this code first (a gated beta — collapses the abuse surface without CAPTCHA/rate-limiting) |
| `RETRIEVAL_LEADS_WEBHOOK` | Durable lead sink URL (e.g. the Google Apps Script `/exec`) — POSTed each signup |
| `RETRIEVAL_LEADS_TOKEN` | Optional shared secret sent as `_token`; the endpoint verifies it to reject spam |

What hosted mode changes:
- Per-session storage root (no shared/global study list).
- "Hosted session" banner + **Clear studies** / **Delete all my data**.
- Expired session workspaces auto-deleted (`DEFAULT_TTL_HOURS`, default 12).
- "Import v1 data" hidden (it reads a local directory).
- The OpenAI key is still session-only, sent directly to OpenAI, never
  stored; there is no app-owned key.

## Streamlit Community Cloud (gated beta)
1. Push this repo to GitHub (public or private).
2. New app → entry point `app.py`, Python **3.12**.
3. In the app's **Settings → Secrets / environment**, set:
   ```
   RETRIEVAL_MODE = "hosted"
   RETRIEVAL_ACCESS_CODE = "your-shared-code"
   ```
   Do **not** set `OPENAI_API_KEY` — users bring their own.
4. Community Cloud's filesystem is ephemeral, which suits hosted sessions;
   nothing persists between restarts by design.

## Lead capture (gated hosted mode)
When `RETRIEVAL_ACCESS_CODE` is set, the entry gate collects **name + email**
(+ optional organisation) and a consent tick before granting access. Each
signup is recorded two ways:

- appended to `data/leads.jsonl` — fine on your own machine / a persistent
  VPS, but **NOT durable on Streamlit Community Cloud** (ephemeral disk);
- POSTed as JSON to `RETRIEVAL_LEADS_WEBHOOK` if set — **this is your durable
  database on an ephemeral host.** Payload:
  `{timestamp, name, email, org, session_id, source}` (+ `_token` when
  `RETRIEVAL_LEADS_TOKEN` is set, so the endpoint can reject spam).

  **Google Sheet recipe (recommended):** the ready-to-paste Apps Script is
  `docs/leads_google_sheet.gs` — create a Sheet, paste it into Extensions ->
  Apps Script, deploy as a Web app (Execute as: Me, Access: Anyone), then set
  `RETRIEVAL_LEADS_WEBHOOK` to the `/exec` URL (and `RETRIEVAL_LEADS_TOKEN` to a
  shared secret). Each signup appends a row to a "Leads" tab.

The OpenAI API key is **never** part of a lead record. On your own machine
you can also read/export leads via `core.leads.read_leads()`.

**Your responsibilities (privacy):** name + email are personal data. Only
collect them with a clear purpose (the consent line states contact + OpenAI
processing), keep `data/leads.jsonl` and your webhook destination secure,
and make sure you have the right to contact people. Add a short privacy
notice if you deploy publicly.

## Not yet built (before a *fully public, anonymous* launch)
This foundation covers isolation, gating and cleanup. A wide-open public
deployment additionally needs: IP/session rate limiting, a request timeout
and retry/backoff surfaced in the UI, run cancel/retry controls, per-session
concurrency caps, a health-check endpoint, friendly outage messages, and the
legal pages (privacy, terms, data-retention, OpenAI-usage, permission-to-
upload). Until those exist, use the **access code** and share the URL only
with a controlled audience.
