"""Access-gate lead capture (name + email) for hosted / gated deployments.

Each signup is recorded to TWO places, best-effort and independent:
  1. an append-only JSONL at data/leads.jsonl  (works on your own machine or a
     persistent VPS; NOT durable on Streamlit Community Cloud, whose disk is
     ephemeral);
  2. an optional webhook (env RETRIEVAL_LEADS_WEBHOOK) — POST the lead as JSON to
     a Google Sheet / Zapier / form endpoint. THIS is your durable database on
     an ephemeral host.

An OpenAI API key is NEVER part of a lead record. Recording never blocks or
breaks the sign-in flow — a failed sink is swallowed.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEADS_PATH = os.path.join(HERE, "data", "leads.jsonl")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Keys that must never appear in a stored lead, defensively stripped.
_FORBIDDEN = ("api_key", "openai_api_key", "key", "token", "secret",
              "password")


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip()))


def webhook_url() -> str:
    return (os.environ.get("RETRIEVAL_LEADS_WEBHOOK") or "").strip()


def _sanitise(lead: dict) -> dict:
    return {k: v for k, v in lead.items()
            if not any(f in k.lower() for f in _FORBIDDEN)}


def record_lead(name: str, email: str, org: str = "", session_id: str = "",
                source: str = "access_gate", extra: dict | None = None) -> dict:
    """Build + persist one lead. Returns the stored record. Never raises."""
    lead = _sanitise({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "name": (name or "").strip(),
        "email": (email or "").strip(),
        "org": (org or "").strip(),
        "session_id": session_id,
        "source": source,
        **(_sanitise(extra) if extra else {}),
    })
    # (1) local append-only JSONL
    try:
        os.makedirs(os.path.dirname(LEADS_PATH), exist_ok=True)
        with open(LEADS_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(lead, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # (2) optional durable webhook. A shared token (RETRIEVAL_LEADS_TOKEN) is
    # sent as `_token` for the endpoint to verify; it is auth, not lead data,
    # so it is NOT written to the local record.
    url = webhook_url()
    if url:
        payload = dict(lead)
        tok = (os.environ.get("RETRIEVAL_LEADS_TOKEN") or "").strip()
        if tok:
            payload["_token"] = tok
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=8).read()
        except Exception:
            pass
    return lead


def read_leads() -> list:
    """All locally-recorded leads (for self-host admin/export)."""
    if not os.path.exists(LEADS_PATH):
        return []
    out = []
    with open(LEADS_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out
