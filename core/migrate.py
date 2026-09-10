"""Migration + sample-data builders.

migrate_legacy(): imports the v1 flat output/ (raw envelopes + config.json)
into the v2 study/dataset structure. The old data was TWO live batches
(gpt-5.6-terra × 25, gpt-5.6-sol × 20 raw) written into one store — they
become two separate datasets of dataset_type='imported' under one study.
Old "client" config maps to a primary tracked entity; the old competitor
list maps to entities with role='competitor' (that is what they were
configured as — roles remain user-editable afterwards).

build_sample_dataset(): creates the clearly-labelled Sample study from the
test fixtures in tests/fixtures.py. Sample rows can never reach a live
dataset — they live in their own study/dataset files.
"""

from __future__ import annotations

import json
import os

from . import datasets as ds
from .entities import new_entity, EntityMatcher
from .prompts import new_prompt, assign_ids

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _legacy_config():
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    return {}


def legacy_pricing() -> dict:
    """Model pricing is application-owned (pricing.json), separate from any
    client/entity configuration; the old config.json 'pricing' block is the
    fallback for un-migrated installs."""
    p = os.path.join(HERE, "pricing.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    return _legacy_config().get("pricing", {})


def _entities_from_legacy(cfg: dict) -> list:
    ents = []
    client = cfg.get("client") or cfg.get("brand") or {}
    if client.get("name"):
        ents.append(new_entity(
            client["name"], primary_domain=(client.get("domains") or [""])[0],
            alt_domains=(client.get("domains") or [])[1:],
            aliases=client.get("name_variants", []),
            role="primary", notes="migrated from v1 'client'"))
    for comp in cfg.get("competitors", []):
        if isinstance(comp, str):
            comp = {"name": comp}
        ents.append(new_entity(
            comp.get("name", ""), primary_domain=(comp.get("domains")
                                                  or [""])[0],
            alt_domains=(comp.get("domains") or [])[1:],
            aliases=comp.get("variants", []),
            role="competitor", notes="migrated from v1 competitor list"))
    return ents


def migrate_legacy(output_dir: str = None, study_name: str =
                   "Melbourne SEO agencies (migrated v1)") -> dict:
    """Returns {study, datasets: [...], observations: n} or raises."""
    output_dir = output_dir or os.path.join(HERE, "output")
    raw_dir = os.path.join(output_dir, "raw")
    if not os.path.isdir(raw_dir):
        raise FileNotFoundError(f"No legacy raw dir at {raw_dir}")

    cfg = _legacy_config()
    pricing = cfg.get("pricing", {})
    entities = _entities_from_legacy(cfg)
    matcher = EntityMatcher(entities) if entities else None

    # Group raw envelopes by requested model — each batch => one dataset.
    batches = {}
    for name in sorted(os.listdir(raw_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(raw_dir, name), encoding="utf-8") as fh:
            env = json.load(fh)
        req = ((env.get("_request") or {}).get("model")
               or (env.get("_meta") or {}).get("model") or "unknown")
        batches.setdefault(req, []).append((name, env))

    # Prompt library from the envelopes.
    seen_prompts = {}
    for envs in batches.values():
        for _, env in envs:
            m = env.get("_meta") or {}
            pid = m.get("prompt_id") or ""
            text = (env.get("_request") or {}).get("prompt_text") \
                or m.get("prompt_text") or ""
            if pid and pid not in seen_prompts:
                seen_prompts[pid] = new_prompt(text, prompt_id=pid)
    prompts = assign_ids([seen_prompts[k] for k in sorted(seen_prompts)])

    study = ds.create_study(
        study_name, mode="multi" if len(entities) > 1 else
        ("single" if entities else "unbranded"),
        entities=entities, prompts=prompts,
        prompt_set_version=cfg.get("prompt_set_version", "v1"),
        notes="Imported from v1 flat output/ (two live batches, one store).")

    made = []
    total = 0
    for req_model, envs in sorted(batches.items()):
        manifest = ds.create_dataset(
            study, "imported", requested_model=req_model,
            runs_per_prompt=max((e[1].get("_meta", {}).get("run_number") or 0)
                                for e in envs),
            label=f"v1 batch {req_model} ({len(envs)} raw)")
        conn = ds.open_db(ds.dataset_db_path(study["study_id"],
                                             manifest["dataset_id"]))
        try:
            for name, env in envs:
                m = env.get("_meta") or {}
                raw = env.get("response")
                meta = {
                    "observation_id": "obs_" + name[:-5].replace(".", "_"),
                    "prompt_id": m.get("prompt_id"),
                    "run_number": m.get("run_number"),
                    "requested_model": req_model,
                    "timestamp": m.get("timestamp"),
                    "location": m.get("location", "Melbourne, Victoria, AU"),
                    "prompt_text": (env.get("_request") or {}).get(
                        "prompt_text", ""),
                    "status": "ok" if raw else "error",
                    "error": env.get("_error") or "",
                    "latency_ms": None,
                }
                ds.store_observation(manifest, meta, raw, matcher, pricing,
                                     conn=conn)
                total += 1
            conn.commit()
        finally:
            conn.close()
        made.append(ds.update_totals(manifest))
    return {"study": study, "datasets": made, "observations": total}


def build_sample_dataset() -> dict:
    """Sample study from labelled test fixtures. Always dataset_type='sample'
    inside its own 'Sample study' — never touches any live dataset."""
    from tests.fixtures import sample_payloads, SAMPLE_ENTITIES, SAMPLE_PROMPTS
    entities = SAMPLE_ENTITIES()
    prompts = SAMPLE_PROMPTS()
    study = ds.create_study("Sample study (fixtures)", mode="multi",
                            entities=entities, prompts=prompts,
                            prompt_set_version="sample-v1",
                            notes="TEST/SAMPLE DATA - fabricated fixtures, "
                                  "not live research.")
    manifest = ds.create_dataset(study, "sample",
                                 requested_model="gpt-5.6-sol",
                                 runs_per_prompt=2, label="sample fixtures")
    matcher = EntityMatcher(entities)
    conn = ds.open_db(ds.dataset_db_path(study["study_id"],
                                         manifest["dataset_id"]))
    try:
        for i, (meta, raw) in enumerate(sample_payloads(), 1):
            meta = dict(meta)
            meta.setdefault("observation_id", f"obs_sample_{i:03d}")
            meta.setdefault("requested_model", "gpt-5.6-sol")
            ds.store_observation(manifest, meta, raw, matcher,
                                 legacy_pricing(), conn=conn)
        conn.commit()
    finally:
        conn.close()
    ds.update_totals(manifest)
    return {"study": study, "manifest": manifest}
