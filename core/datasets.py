"""Study + dataset lifecycle and physical isolation.

Directory layout (spec §13):

    data/studies/{study_id}/study.json
    data/studies/{study_id}/{dataset_id}/
        manifest.json      dataset identity + settings + totals
        prompts.json       frozen prompt list used by this dataset
        entities.json      frozen entity list used by this dataset
        observations.db    v2 SQLite (this dataset ONLY)
        raw/{observation_id}.json
        exports/

Isolation model: one SQLite per dataset. A live dataset physically cannot
contain sample rows — they are different files. Sample data always goes to a
dedicated dataset with dataset_type='sample' inside a clearly named study.
Raw responses are written before flattening; `reprocess()` rebuilds every
table from raw without any API call.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone

from . import APP_VERSION
from .model import SCHEMA_VERSION, open_db, insert, delete_observation
from .entities import EntityMatcher
from .extraction import extract_observation, compute_cost
from .classify import classify_query

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Default persistent root (local mode + tests). Hosted mode overrides this
# PER SESSION via set_session_root() so concurrent visitors never share data.
DATA_ROOT = os.path.join(HERE, "data", "studies")
_session_root = contextvars.ContextVar("fanout_session_root", default=None)


def set_session_root(path: str | None):
    """Scope all study/dataset storage to `path` for the current execution
    context (one Streamlit session's script run). None = use DATA_ROOT."""
    _session_root.set(path)


def data_root() -> str:
    """The active studies root: the per-session override if set, else the
    module default DATA_ROOT (honoured by tests that patch DATA_ROOT)."""
    return _session_root.get() or DATA_ROOT


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "study").lower()).strip("-")
    return s[:40] or "study"


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
            stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


def validate_study(study: dict) -> list:
    """Blocking issues for a run: mode/entity mismatch, duplicate prompt or
    entity ids. Returns a list of human-readable problems (empty = ok)."""
    problems = []
    mode = study.get("mode", "unbranded")
    entities = study.get("entities", [])
    if mode == "unbranded" and entities:
        problems.append(f"Unbranded study must track no entities "
                        f"(has {len(entities)}). Change mode or remove them.")
    if mode == "single" and len(entities) != 1:
        problems.append(f"Single-entity study must track exactly one entity "
                        f"(has {len(entities)}).")
    if mode == "multi" and len(entities) < 2:
        problems.append(f"Multi-entity study needs at least two entities "
                        f"(has {len(entities)}).")
    eids = [e.get("entity_id") for e in entities]
    dupe = sorted({x for x in eids if eids.count(x) > 1})
    if dupe:
        problems.append(f"Duplicate entity ids: {dupe}")
    primaries = [e for e in entities if e.get("role") == "primary"]
    if len(primaries) > 1:
        problems.append(f"More than one primary entity "
                        f"({[e['name'] for e in primaries]}).")
    pids = [p.get("prompt_id") for p in study.get("prompts", [])]
    pdupe = sorted({x for x in pids if pids.count(x) > 1})
    if pdupe:
        problems.append(f"Duplicate prompt ids: {pdupe}")
    return problems


# Common country name -> ISO-3166-1 alpha-2 (the API's user_location.country
# wants a 2-letter code). Extend as needed; unknown names pass through as-is.
_COUNTRY_ALIASES = {
    "australia": "AU", "united states": "US", "usa": "US", "us": "US",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "new zealand": "NZ", "canada": "CA", "ireland": "IE", "india": "IN",
    "singapore": "SG", "germany": "DE", "france": "FR", "spain": "ES",
    "italy": "IT", "netherlands": "NL", "japan": "JP", "china": "CN",
    "south africa": "ZA", "brazil": "BR", "mexico": "MX",
    "united arab emirates": "AE", "uae": "AE",
}


def _norm_country(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return ""
    low = t.lower()
    if low in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[low]
    return t.upper() if len(t) == 2 else t


def parse_location_string(s: str):
    """Flexible approximate-location parser. The OpenAI user_location fields
    (city/region/country) are all optional, so accept any of:
        'Melbourne, Victoria, AU'  -> city + region + country
        'Sydney, AU'               -> city + country
        'AU' / 'Australia'         -> country only
    Country name -> ISO2 where known. Returns a dict with only the present
    keys, or None if empty. The last comma-part is always the country."""
    parts = [p.strip() for p in (s or "").split(",") if p.strip()]
    if not parts:
        return None
    loc = {"country": _norm_country(parts[-1])}
    if len(parts) == 2:
        loc["city"] = parts[0]
    elif len(parts) >= 3:
        loc["city"] = parts[0]
        loc["region"] = parts[1]
    return loc


def format_location(loc: dict) -> str:
    """Human/label form from a (possibly partial) location dict."""
    return ", ".join(loc[k] for k in ("city", "region", "country")
                     if loc.get(k))


def prompt_set_hash(prompts: list) -> str:
    texts = sorted((p.get("prompt_text", "") if isinstance(p, dict) else str(p))
                   for p in prompts)
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Studies
# --------------------------------------------------------------------------- #

def create_study(name: str, mode: str = "unbranded", entities=None,
                 prompts=None, locations=None, notes: str = "",
                 prompt_set_version: str = "v1") -> dict:
    study_id = f"{_slug(name)}-{uuid.uuid4().hex[:6]}"
    study = {
        "study_id": study_id, "study_name": name, "mode": mode,
        "entities": entities or [], "prompts": prompts or [],
        "locations": locations or [{"city": "Melbourne", "region": "Victoria",
                                    "country": "AU"}],
        "prompt_set_version": prompt_set_version,
        "notes": notes, "created_at": _now(),
    }
    save_study(study)
    return study


def study_dir(study_id: str) -> str:
    return os.path.join(data_root(), study_id)


def save_study(study: dict):
    d = study_dir(study["study_id"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "study.json"), "w", encoding="utf-8") as fh:
        json.dump(study, fh, ensure_ascii=False, indent=2)


def load_study(study_id: str) -> dict:
    with open(os.path.join(study_dir(study_id), "study.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def list_studies() -> list:
    root = data_root()
    if not os.path.isdir(root):
        return []
    out = []
    for sid in sorted(os.listdir(root)):
        p = os.path.join(root, sid, "study.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except Exception:
                continue
    return out


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #

def create_dataset(study: dict, dataset_type: str, requested_model: str,
                   runs_per_prompt: int, search_mode: str = "web_search:auto",
                   reasoning: dict | None = None, label: str = "",
                   search_settings: dict | None = None) -> dict:
    assert dataset_type in ("live", "sample", "imported"), dataset_type
    dataset_id = (f"{dataset_type}-"
                  f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
                  f"{uuid.uuid4().hex[:4]}")
    d = os.path.join(study_dir(study["study_id"]), dataset_id)
    os.makedirs(os.path.join(d, "raw"), exist_ok=True)
    os.makedirs(os.path.join(d, "exports"), exist_ok=True)
    manifest = {
        "dataset_id": dataset_id, "dataset_type": dataset_type,
        "study_id": study["study_id"], "study_name": study["study_name"],
        "label": label, "created_at": _now(),
        "requested_model": requested_model, "actual_models": [],
        "runs_per_prompt": runs_per_prompt, "search_mode": search_mode,
        "reasoning": reasoning or {},
        "search_settings": search_settings or {},
        "locations": study.get("locations", []),
        "prompt_set_version": study.get("prompt_set_version", "v1"),
        "prompt_set_hash": prompt_set_hash(study.get("prompts", [])),
        "schema_version": SCHEMA_VERSION, "app_version": APP_VERSION,
        "git_commit": git_commit(),
        "totals": {"observations": 0, "ok": 0, "failed": 0, "cost_usd": 0.0},
    }
    _write_json(os.path.join(d, "manifest.json"), manifest)
    _write_json(os.path.join(d, "prompts.json"), study.get("prompts", []))
    _write_json(os.path.join(d, "entities.json"), study.get("entities", []))
    open_db(os.path.join(d, "observations.db")).close()
    return manifest


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def dataset_dir(study_id: str, dataset_id: str) -> str:
    return os.path.join(study_dir(study_id), dataset_id)


def dataset_db_path(study_id: str, dataset_id: str) -> str:
    return os.path.join(dataset_dir(study_id, dataset_id), "observations.db")


def load_manifest(study_id: str, dataset_id: str) -> dict:
    with open(os.path.join(dataset_dir(study_id, dataset_id),
                           "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def save_manifest(manifest: dict):
    _write_json(os.path.join(dataset_dir(manifest["study_id"],
                                         manifest["dataset_id"]),
                             "manifest.json"), manifest)


def list_datasets(study_id: str) -> list:
    d = study_dir(study_id)
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name, "manifest.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except Exception:
                continue
    return out


def dataset_entities(study_id: str, dataset_id: str) -> list:
    p = os.path.join(dataset_dir(study_id, dataset_id), "entities.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return []


def dataset_prompts(study_id: str, dataset_id: str) -> list:
    p = os.path.join(dataset_dir(study_id, dataset_id), "prompts.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return []


# --------------------------------------------------------------------------- #
# Storing one observation (raw first, then flatten)
# --------------------------------------------------------------------------- #

def store_observation(manifest: dict, meta: dict, raw: dict | None,
                      matcher: EntityMatcher | None, pricing: dict,
                      conn=None) -> dict:
    """Persist one run. `meta` needs: observation_id, prompt_id, run_number,
    requested_model, timestamp, location, prompt_text, status, error,
    latency_ms. Raw (if any) is written to raw/{observation_id}.json FIRST.
    Returns the stored observation row (dict)."""
    ddir = dataset_dir(manifest["study_id"], manifest["dataset_id"])
    obs_id = meta["observation_id"]
    if raw is not None:
        envelope = {"_meta": {k: meta.get(k) for k in (
            "observation_id", "prompt_id", "run_number", "requested_model",
            "timestamp", "location", "prompt_text", "latency_ms")},
            "_error": meta.get("error") or None,
            "response": raw}
        _write_json(os.path.join(ddir, "raw", f"{obs_id}.json"), envelope)

    ex = (extract_observation(raw, matcher) if raw is not None else None)
    usage = ex["usage"] if ex else {}
    actual = ex["actual_model"] if ex else ""
    cost = (compute_cost(actual or meta.get("requested_model", ""), usage,
                         pricing, ex["rollups"]["num_search_actions"])
            if ex else None)

    # Classify incomplete API responses: a response whose status is not
    # 'completed' is never counted as ok — its data is stored (evidence) but
    # flagged so rates exclude it.
    status = meta.get("status", "ok")
    error = meta.get("error", "")
    if ex and status == "ok":
        rs = ex.get("response_status") or ""
        if rs == "failed":
            # A failed response body keeps its ACTUAL API error.
            status = "error"
            error = (f"api_error {ex.get('api_error_code') or '?'}: "
                     f"{ex.get('api_error_message') or 'no message'}")
        elif rs and rs != "completed":
            status = "incomplete"
            error = (f"response_status={rs}"
                     + (f"; reason={ex.get('incomplete_reason')}"
                        if ex.get("incomplete_reason") else ""))

    row = {
        "observation_id": obs_id,
        "dataset_id": manifest["dataset_id"], "study_id": manifest["study_id"],
        "prompt_id": meta.get("prompt_id"),
        "run_number": meta.get("run_number"),
        "requested_model": meta.get("requested_model"),
        "actual_model": actual,
        "response_id": ex["response_id"] if ex else "",
        "response_status": ex["response_status"] if ex else "",
        "incomplete_reason": ex["incomplete_reason"] if ex else "",
        "response_created_at": ex["response_created_at"] if ex else "",
        "service_tier": ex["service_tier"] if ex else "",
        "api_error_code": ex["api_error_code"] if ex else "",
        "api_error_message": ex["api_error_message"] if ex else "",
        "timestamp": meta.get("timestamp"),
        "location": meta.get("location"),
        "search_mode": manifest.get("search_mode"),
        "prompt_set_version": manifest.get("prompt_set_version"),
        "schema_version": SCHEMA_VERSION,
        "status": status, "error": error,
        "prompt_text": meta.get("prompt_text", ""),
        "answer_text": ex["answer_text"] if ex else "",
        "reasoning_summary": ex["reasoning_summary"] if ex else None,
        "cost_usd": cost, "latency_ms": meta.get("latency_ms"),
    }
    row.update(usage)
    row.update(ex["rollups"] if ex else {})

    own = conn is None
    if own:
        conn = open_db(dataset_db_path(manifest["study_id"],
                                       manifest["dataset_id"]))
    try:
        delete_observation(conn, obs_id)
        insert(conn, "observations", row)
        ids = {"observation_id": obs_id, "dataset_id": manifest["dataset_id"],
               "study_id": manifest["study_id"]}
        if ex:
            for e in ex["events"]:
                insert(conn, "trace_events",
                       {**{k: ids[k] for k in ("observation_id", "dataset_id")},
                        **e})
            for q in ex["fanout_queries"]:
                q = dict(q)
                q["taxonomy"] = classify_query(q.get("query") or "", matcher,
                                               q.get("site_domain") or "")
                insert(conn, "fanout_queries", {**ids, **q})
            for s in ex["search_sources"]:
                insert(conn, "search_sources", {**ids, **s})
            for o in ex["opened_pages"]:
                insert(conn, "opened_pages", {**ids, **o})
            for i in ex["inpage_searches"]:
                insert(conn, "inpage_searches", {**ids, **i})
            for c in ex["citations"]:
                insert(conn, "citations", {**ids, **c})
            for c in ex["citation_occurrences"]:
                insert(conn, "citation_occurrences", {**ids, **c})
            for c in ex["search_calls"]:
                insert(conn, "search_calls", {**ids, **c})
            for se in ex["source_entity"]:
                insert(conn, "source_entity", {**ids, **se})
            for er in ex["entity_runs"]:
                insert(conn, "entity_runs", {**ids, **er})
        conn.commit()
    finally:
        if own:
            conn.close()
    return row


def update_totals(manifest: dict):
    conn = open_db(dataset_db_path(manifest["study_id"],
                                   manifest["dataset_id"]))
    try:
        n, ok, cost = conn.execute(
            "SELECT COUNT(*), SUM(status='ok'), SUM(COALESCE(cost_usd,0)) "
            "FROM observations").fetchone()
        models = [r[0] for r in conn.execute(
            "SELECT DISTINCT actual_model FROM observations "
            "WHERE actual_model IS NOT NULL AND actual_model<>''")]
    finally:
        conn.close()
    manifest["totals"] = {"observations": n or 0, "ok": ok or 0,
                          "failed": (n or 0) - (ok or 0),
                          "cost_usd": round(cost or 0.0, 4)}
    manifest["actual_models"] = models
    save_manifest(manifest)
    return manifest


# --------------------------------------------------------------------------- #
# Reprocess from raw (rules changed? no API calls needed)
# --------------------------------------------------------------------------- #

def reprocess(study_id: str, dataset_id: str, pricing: dict) -> int:
    """Rebuild every table from raw/*.json using current extraction and
    classification rules. Returns number of observations rebuilt."""
    manifest = load_manifest(study_id, dataset_id)
    entities = dataset_entities(study_id, dataset_id)
    matcher = EntityMatcher(entities) if entities else None
    ddir = dataset_dir(study_id, dataset_id)
    raw_dir = os.path.join(ddir, "raw")
    conn = open_db(dataset_db_path(study_id, dataset_id))
    count = 0
    try:
        # Failed raw-less observations keep their rows untouched — only
        # observations that have a raw file are rebuilt below.
        for name in sorted(os.listdir(raw_dir)) if os.path.isdir(raw_dir) else []:
            if not name.endswith(".json"):
                continue
            obs_id = name[:-5]
            with open(os.path.join(raw_dir, name), encoding="utf-8") as fh:
                env = json.load(fh)
            raw = env.get("response") if isinstance(env, dict) and \
                "response" in env else env
            meta = env.get("_meta", {}) if isinstance(env, dict) else {}
            meta = {
                "observation_id": obs_id,
                "prompt_id": meta.get("prompt_id"),
                "run_number": meta.get("run_number"),
                "requested_model": meta.get("requested_model")
                                   or meta.get("model")
                                   or manifest.get("requested_model"),
                "timestamp": meta.get("timestamp"),
                "location": meta.get("location"),
                "prompt_text": meta.get("prompt_text")
                               or (env.get("_request", {}) or {}).get(
                                   "prompt_text", ""),
                "status": "ok" if raw else "error",
                "error": (env.get("_error") or "") if isinstance(env, dict)
                         else "",
                "latency_ms": meta.get("latency_ms"),
            }
            store_observation(manifest, meta, raw, matcher, pricing, conn=conn)
            count += 1
        conn.commit()
    finally:
        conn.close()
    update_totals(manifest)
    return count
