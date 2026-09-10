"""Live run engine.

Model integrity (spec §1):
- requested_model is what the user chose; actual_model comes from the API
  response's `model` field.
- If the API returns a DIFFERENT model (not the requested id or a dated
  snapshot of it), the run is recorded as failed with `model_mismatch` and
  the whole batch ABORTS — we never continue under a substituted model.
- If the model id is rejected outright, the batch stops with the API error.

Reasoning: we request `reasoning={"summary": "auto"}` (plus optional effort)
so reasoning summaries are captured where the model supports them. If the API
rejects the reasoning parameter itself (400 naming it), we retry that call
once WITHOUT the parameter — that is a parameter downgrade, recorded in the
manifest as reasoning_supported=false; it is never a model substitution.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from .entities import EntityMatcher
from . import datasets as ds


def model_matches(requested: str, actual: str) -> bool:
    """True when `actual` is the requested id or a dated snapshot of it
    (e.g. requested 'gpt-5.6-sol', actual 'gpt-5.6-sol-2026-07-01')."""
    if not requested or not actual:
        return False
    r, a = requested.lower(), actual.lower()
    return a == r or a.startswith(r + "-") or a.startswith(r + ":")


class RunAborted(Exception):
    pass


def _reasoning_param(reasoning_cfg: dict | None):
    if not reasoning_cfg:
        return None
    p = {}
    if reasoning_cfg.get("summary"):
        p["summary"] = reasoning_cfg["summary"]
    if reasoning_cfg.get("effort"):
        p["effort"] = reasoning_cfg["effort"]
    return p or None


def run_batch(client, study: dict, manifest: dict, pricing: dict,
              include_sources: bool = True, progress=None,
              sleep_between: float = 0.0) -> dict:
    """Execute prompts × runs (× locations) into the manifest's dataset.

    `client`: an OpenAI-compatible client (client.responses.create).
    `progress(info)`: optional callback with
        {done, total, prompt_id, run_number, location, ok, failed, cost,
         elapsed_s, message}
    Returns summary {ok, failed, cost, aborted, abort_reason}.
    Raises nothing for per-run errors (logged + stored); aborts loop only on
    model integrity failure or model rejection.
    """
    problems = ds.validate_study(study)
    if problems:
        return {"ok": 0, "failed": 0, "cost": 0.0, "aborted": True,
                "abort_reason": "Study validation failed: "
                                + " | ".join(problems),
                "reasoning_supported": None}

    prompts = [p for p in study.get("prompts", [])
               if p.get("active", True)]
    locations = study.get("locations") or [{"city": "Melbourne",
                                            "region": "Victoria",
                                            "country": "AU"}]
    runs_per_prompt = int(manifest.get("runs_per_prompt", 1))
    requested = manifest["requested_model"]
    reasoning_cfg = dict(manifest.get("reasoning") or {})
    entities = study.get("entities", [])
    matcher = EntityMatcher(entities) if entities else None

    # Prompt-level location override: an overridden prompt runs ONLY at its
    # own location (once per repeat), not once per study location.
    def locations_for(prompt):
        ov = ds.parse_location_string(prompt.get("location", ""))
        return [ov] if ov else locations

    total = sum(len(locations_for(p)) for p in prompts) * runs_per_prompt
    search_cfg = dict(manifest.get("search_settings") or {})
    done = n_ok = n_failed = 0
    cost_total = 0.0
    t0 = time.time()
    aborted, abort_reason = False, ""
    reasoning_supported = True

    conn = ds.open_db(ds.dataset_db_path(manifest["study_id"],
                                         manifest["dataset_id"]))

    def note(msg, pid="", rn=0, loc=""):
        if progress:
            progress({"done": done, "total": total, "prompt_id": pid,
                      "run_number": rn, "location": loc, "ok": n_ok,
                      "failed": n_failed, "cost": round(cost_total, 4),
                      "elapsed_s": int(time.time() - t0), "message": msg})

    results_supported = bool(search_cfg.get("include_results"))

    def call(prompt_text, loc):
        nonlocal reasoning_supported, results_supported
        tool = {"type": "web_search",
                "user_location": {"type": "approximate", **loc}}
        if search_cfg.get("search_context_size") in ("low", "medium", "high"):
            tool["search_context_size"] = search_cfg["search_context_size"]
        filters = {}
        if search_cfg.get("allowed_domains"):
            filters["allowed_domains"] = list(search_cfg["allowed_domains"])
        if filters:
            tool["filters"] = filters
        kwargs = dict(
            model=requested,
            input=prompt_text,
            tools=[tool],
            tool_choice=(search_cfg.get("tool_choice")
                         if search_cfg.get("tool_choice") in ("auto",
                                                              "required")
                         else "auto"),
        )
        if search_cfg.get("max_tool_calls"):
            kwargs["max_tool_calls"] = int(search_cfg["max_tool_calls"])
        if include_sources:
            inc = ["web_search_call.action.sources"]
            if results_supported:
                # Feature-detected optional capture (documented for image
                # search results; not guaranteed on every API version).
                inc.append("web_search_call.results")
            kwargs["include"] = inc
        rp = _reasoning_param(reasoning_cfg) if reasoning_supported else None
        if rp:
            kwargs["reasoning"] = rp
        try:
            return client.responses.create(**kwargs)
        except Exception as e:
            msg = str(e).lower()
            param_err = ("400" in msg or "invalid" in msg
                         or "unsupported" in msg or "unknown" in msg)
            if rp and "reasoning" in msg and param_err:
                # Parameter (not model) unsupported — retry once without it.
                reasoning_supported = False
                kwargs.pop("reasoning", None)
                return client.responses.create(**kwargs)
            if results_supported and "results" in msg and param_err:
                results_supported = False
                kwargs["include"] = ["web_search_call.action.sources"]
                return client.responses.create(**kwargs)
            raise

    try:
        for prompt in prompts:
            if aborted:
                break
            for loc in locations_for(prompt):
                if aborted:
                    break
                loc_str = ds.format_location(loc)
                for run_no in range(1, runs_per_prompt + 1):
                    if aborted:
                        break
                    done += 1
                    obs_id = "obs_" + uuid.uuid4().hex[:12]
                    ts = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    meta = {
                        "observation_id": obs_id,
                        "prompt_id": prompt.get("prompt_id"),
                        "run_number": run_no,
                        "requested_model": requested,
                        "timestamp": ts, "location": loc_str,
                        "prompt_text": prompt.get("prompt_text", ""),
                        "status": "ok", "error": "", "latency_ms": None,
                    }
                    note("calling", prompt.get("prompt_id"), run_no, loc_str)
                    t1 = time.time()
                    raw = None
                    try:
                        resp = call(prompt.get("prompt_text", ""),
                                    {k: loc[k] for k in
                                     ("city", "region", "country")
                                     if loc.get(k)})
                        try:
                            raw = resp.model_dump(mode="json")
                        except Exception:
                            import json as _j
                            raw = _j.loads(resp.model_dump_json())
                    except Exception as e:
                        meta["status"] = "error"
                        meta["error"] = f"{type(e).__name__}: {e}"[:800]
                        low = str(e).lower()
                        if ("model" in low and (
                                "not found" in low or "does not exist" in low
                                or "invalid" in low or "unsupported" in low)):
                            aborted = True
                            abort_reason = (f"Model '{requested}' rejected by "
                                            f"the API: {e}")
                    meta["latency_ms"] = int((time.time() - t1) * 1000)

                    if raw is not None:
                        actual = raw.get("model") or ""
                        if actual and not model_matches(requested, actual):
                            meta["status"] = "error"
                            meta["error"] = (f"model_mismatch: requested "
                                             f"{requested}, got {actual}")
                            aborted = True
                            abort_reason = meta["error"]

                    row = ds.store_observation(manifest, meta, raw, matcher,
                                               pricing, conn=conn)
                    if row.get("status") == "ok":
                        n_ok += 1
                        cost_total += float(row.get("cost_usd") or 0)
                    else:
                        n_failed += 1
                    note("stored", prompt.get("prompt_id"), run_no, loc_str)
                    if sleep_between > 0:
                        time.sleep(sleep_between)
    finally:
        conn.close()
        manifest["reasoning"]["supported"] = reasoning_supported
        if "include_results" in search_cfg:
            manifest.setdefault("search_settings", {})[
                "include_results_supported"] = results_supported
        ds.update_totals(manifest)

    return {"ok": n_ok, "failed": n_failed, "cost": round(cost_total, 4),
            "aborted": aborted, "abort_reason": abort_reason,
            "reasoning_supported": reasoning_supported}
