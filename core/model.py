"""Constants, URL canonicalisation and the v2 SQLite schema.

Design rules the schema enforces:
- One SQLite file per DATASET (physical isolation — a live dataset cannot
  contain sample rows because sample rows live in a different file).
- Every row carries dataset_id + study_id + observation_id, so exports can
  re-verify membership even though isolation is already physical.
- Raw data is immutable: raw/{observation_id}.json is written before any
  flattening; every table here can be rebuilt from raw (datasets.reprocess).
"""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

SCHEMA_VERSION = "2.2"

SITE_OP_RE = re.compile(r'site:\s*([^\s"\'\)\]]+)', re.IGNORECASE)

# Tracking params stripped for canonical aggregation (raw_url is preserved).
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref", "srsltid",
}

DATASET_TYPES = ("live", "sample", "imported")
ENTITY_ROLES = ("primary", "comparison", "competitor", "neutral")
STUDY_MODES = ("unbranded", "single", "multi")


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #

def domain_of(url: str) -> str:
    """Lower-cased, www-stripped registrable host (best effort)."""
    if not url:
        return ""
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    try:
        host = (urlparse(u).netloc or "").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0].split("@")[-1]


def canonical_url(url: str) -> str:
    """Canonical form for aggregation: scheme->https, host lowercased and
    www-stripped, tracking params removed, fragment dropped, trailing slash
    normalised. The original raw_url is always stored alongside."""
    if not url:
        return ""
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    try:
        p = urlparse(u)
    except Exception:
        return url
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in _TRACKING_PARAMS]
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse(("https", host, path, "", urlencode(q), ""))


def domain_matches(host: str, domain: str) -> bool:
    host = (host or "").lower()
    domain = (domain or "").lower().lstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


# --------------------------------------------------------------------------- #
# SQLite schema (per dataset)
# --------------------------------------------------------------------------- #

_DDL = [
    """CREATE TABLE IF NOT EXISTS observations (
        observation_id TEXT PRIMARY KEY,
        dataset_id TEXT NOT NULL, study_id TEXT NOT NULL,
        prompt_id TEXT, run_number INTEGER,
        requested_model TEXT, actual_model TEXT,
        response_id TEXT, response_status TEXT, incomplete_reason TEXT,
        response_created_at TEXT, service_tier TEXT,
        api_error_code TEXT, api_error_message TEXT,
        timestamp TEXT, location TEXT, search_mode TEXT,
        prompt_set_version TEXT, schema_version TEXT,
        status TEXT, error TEXT,
        prompt_text TEXT, answer_text TEXT,
        reasoning_summary TEXT,
        input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
        reasoning_tokens INTEGER, cached_tokens INTEGER,
        cost_usd REAL, latency_ms INTEGER,
        num_search_actions INTEGER, num_queries INTEGER,
        num_queries_missing_text INTEGER,
        num_site_queries INTEGER, num_open_page INTEGER,
        num_find_in_page INTEGER, num_search_sources INTEGER,
        num_citations INTEGER, num_unique_cited_urls INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS trace_events (
        observation_id TEXT, dataset_id TEXT, seq INTEGER,
        event_type TEXT, payload_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fanout_queries (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        seq INTEGER, batch_index INTEGER,
        query TEXT, query_missing INTEGER,
        has_site_operator INTEGER, site_domain TEXT,
        entity_hits TEXT, taxonomy TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS search_sources (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        seq INTEGER, action_seq INTEGER,
        raw_url TEXT, canonical_url TEXT, domain TEXT, title TEXT,
        source_type TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS opened_pages (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        seq INTEGER, raw_url TEXT, canonical_url TEXT, domain TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS inpage_searches (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        seq INTEGER, raw_url TEXT, canonical_url TEXT, domain TEXT,
        pattern TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS citations (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        raw_url TEXT, canonical_url TEXT, domain TEXT, title TEXT,
        citation_count INTEGER, first_annotation_index INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS citation_occurrences (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        occurrence_number INTEGER,
        raw_url TEXT, canonical_url TEXT, domain TEXT, title TEXT,
        start_index INTEGER, end_index INTEGER,
        cited_text TEXT, answer_sentence TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS search_calls (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        sequence INTEGER, search_call_id TEXT, search_call_status TEXT,
        action_type TEXT, query_count INTEGER, source_count INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS source_entity (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        canonical_url TEXT, domain TEXT, entity_id TEXT,
        relation TEXT, evidence_stage TEXT, evidence TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS entity_runs (
        observation_id TEXT, dataset_id TEXT, study_id TEXT,
        entity_id TEXT,
        mentioned_anywhere INTEGER, named_in_answer INTEGER,
        search_targeted INTEGER,
        source_returned INTEGER, page_opened INTEGER, page_searched INTEGER,
        cited INTEGER
    )""",
]

_INDEXES = [
    ("trace_events", "observation_id"), ("fanout_queries", "observation_id"),
    ("search_sources", "observation_id"), ("opened_pages", "observation_id"),
    ("inpage_searches", "observation_id"), ("citations", "observation_id"),
    ("source_entity", "entity_id"), ("entity_runs", "entity_id"),
    ("citations", "canonical_url"), ("search_sources", "canonical_url"),
]

TABLES = ["observations", "trace_events", "fanout_queries", "search_sources",
          "opened_pages", "inpage_searches", "citations",
          "citation_occurrences", "search_calls", "source_entity",
          "entity_runs"]


# Columns added after 2.0 — applied to older per-dataset DBs on open so a
# reprocess-from-raw can fill them without recreating the file.
_MIGRATIONS = {
    "observations": [
        ("response_id", "TEXT"), ("response_status", "TEXT"),
        ("incomplete_reason", "TEXT"), ("response_created_at", "TEXT"),
        ("service_tier", "TEXT"), ("api_error_code", "TEXT"),
        ("api_error_message", "TEXT")],
    "entity_runs": [("source_returned", "INTEGER"),
                    ("page_opened", "INTEGER"),
                    ("page_searched", "INTEGER")],
    "source_entity": [("evidence_stage", "TEXT")],
    "search_sources": [("source_type", "TEXT")],
}


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for ddl in _DDL:
        conn.execute(ddl)
    for tbl, cols in _MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
        for col, typ in cols:
            if col not in have:
                conn.execute(f'ALTER TABLE {tbl} ADD COLUMN "{col}" {typ}')
    for tbl, col in _INDEXES:
        conn.execute(f'CREATE INDEX IF NOT EXISTS ix_{tbl}_{col} '
                     f'ON {tbl}("{col}")')
    conn.commit()
    return conn


def _norm(v):
    if isinstance(v, bool):
        return 1 if v else 0
    if v == "":
        return None
    return v


def insert(conn, table: str, row: dict):
    cols = list(row.keys())
    conn.execute(
        f'INSERT INTO {table} ({", ".join(chr(34)+c+chr(34) for c in cols)}) '
        f'VALUES ({", ".join("?" for _ in cols)})',
        [_norm(row[c]) for c in cols])


def delete_observation(conn, observation_id: str):
    """Remove one observation and all children (used by idempotent reprocess)."""
    for t in TABLES:
        conn.execute(f'DELETE FROM {t} WHERE observation_id = ?',
                     (observation_id,))
