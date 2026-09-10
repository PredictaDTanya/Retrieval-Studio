"""Export system: study workbook, CSVs, raw ZIP, reconciliation.

Every export reads ONE dataset db. Reconciliation (spec §12) runs before the
workbook is produced; on failure no workbook is written.
Raw data stays immutable — everything here is read-only over the dataset.
"""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from datetime import datetime, timezone

from .model import open_db
from . import datasets as ds
from . import analysis
from .classify import classify_domain

WORKBOOK_SHEETS = [
    "Overview", "Study Manifest", "Prompt Library", "Tracked Entities",
    "Runs", "Reasoning Summaries", "Search Trace", "Fan-Out Queries",
    "Search Sources", "Opened Pages", "In-Page Searches", "Citations",
    "Citation Occurrences", "Search Calls",
    "Source-Entity Links", "Entity Visibility", "Prompt Coverage",
    "Citation Gaps", "Query Clusters", "Flags", "Errors",
]

_ID_COLS = ["dataset_id", "study_id", "observation_id"]


def _formula_safe(v):
    """Neutralise spreadsheet formula injection: user-supplied text starting
    with = + - @ (or a control char) is prefixed with a single quote so a
    reopened CSV/XLSX treats it as text, never an executable formula."""
    if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t",
                                             "\r", "\n"):
        return "'" + v
    return v


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #

def reconcile(study_id: str, dataset_id: str) -> list:
    """Returns [(check_name, ok_bool, detail_str)]. Export only when all ok."""
    manifest = ds.load_manifest(study_id, dataset_id)
    conn = open_db(ds.dataset_db_path(study_id, dataset_id))
    checks = []
    try:
        def add(name, ok, detail=""):
            checks.append((name, bool(ok), detail))

        dsids = [r["dataset_id"] for r in
                 _rows(conn, "SELECT DISTINCT dataset_id FROM observations")]
        add("single dataset_id in observations",
            dsids in ([], [dataset_id]),
            f"found {dsids}")
        for t in ("fanout_queries", "search_sources", "opened_pages",
                  "inpage_searches", "citations", "source_entity",
                  "entity_runs", "trace_events"):
            bad = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE dataset_id<>?",
                (dataset_id,)).fetchone()[0]
            add(f"no foreign dataset rows in {t}", bad == 0, f"{bad} foreign")
        n, okn = conn.execute(
            "SELECT COUNT(*), SUM(status='ok') FROM observations").fetchone()
        okn = okn or 0
        t = manifest.get("totals", {})
        add("run count matches manifest", n == t.get("observations", -1),
            f"db={n} manifest={t.get('observations')}")
        add("failed runs reconcile",
            (n - okn) == t.get("failed", -1),
            f"db={n - okn} manifest={t.get('failed')}")
        # rollup vs row-count reconciliation
        for rollup, table, where in [
                ("num_queries", "fanout_queries", "query_missing=0"),
                ("num_site_queries", "fanout_queries", "has_site_operator=1"),
                ("num_open_page", "opened_pages", "1=1"),
                ("num_find_in_page", "inpage_searches", "1=1"),
                ("num_search_sources", "search_sources", "1=1")]:
            # Children exist for every row that had a raw response
            # (ok, incomplete, or a stored failure) — compare like-for-like.
            a = conn.execute(f"SELECT SUM(COALESCE({rollup},0)) FROM "
                             f"observations").fetchone()[0] or 0
            b = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}"
                             ).fetchone()[0] or 0
            add(f"{rollup} reconciles with {table}", a == b, f"{a} vs {b}")
        a = conn.execute("SELECT SUM(COALESCE(num_citations,0)) FROM "
                         "observations").fetchone()[0] or 0
        b = conn.execute("SELECT SUM(COALESCE(citation_count,0)) FROM "
                         "citations").fetchone()[0] or 0
        add("citation totals reconcile", a == b, f"{a} vs {b}")
        c_occ = conn.execute("SELECT COUNT(*) FROM citation_occurrences"
                             ).fetchone()[0] or 0
        add("citation occurrences match citation totals", a == c_occ,
            f"{a} vs {c_occ}")
        sc = conn.execute("SELECT COUNT(*) FROM search_calls WHERE "
                          "action_type='search'").fetchone()[0] or 0
        na = conn.execute("SELECT SUM(COALESCE(num_search_actions,0)) "
                          "FROM observations").fetchone()[0] or 0
        add("search-call provenance matches search actions", sc == na,
            f"{sc} vs {na}")
        # prompt reconciliation
        p_db = {r["prompt_id"] for r in
                _rows(conn, "SELECT DISTINCT prompt_id FROM observations")}
        p_ds = {p.get("prompt_id") for p in
                ds.dataset_prompts(study_id, dataset_id)}
        add("prompt ids subset of dataset prompt list",
            p_db.issubset(p_ds) if p_ds else True,
            f"unknown={sorted(p_db - p_ds)[:5]}" if p_ds else "no frozen list")
        # entity reconciliation
        e_db = {r["entity_id"] for r in
                _rows(conn, "SELECT DISTINCT entity_id FROM entity_runs")}
        e_ds = {e.get("entity_id") for e in
                ds.dataset_entities(study_id, dataset_id)}
        add("entity ids subset of dataset entity list",
            e_db.issubset(e_ds) if (e_db or e_ds) else True,
            f"unknown={sorted(e_db - e_ds)[:5]}")
        # model integrity
        # Manifest records every actual model observed (any status);
        # the consistency-with-requested check below stays ok-only, so a
        # failed model_mismatch row can't poison it.
        models = [r["actual_model"] for r in _rows(
            conn, "SELECT DISTINCT actual_model FROM observations "
                  "WHERE actual_model IS NOT NULL AND actual_model<>''")]
        man_models = manifest.get("actual_models", [])
        add("actual models match manifest",
            sorted(models) == sorted(man_models),
            f"db={models} manifest={man_models}")
        req = manifest.get("requested_model", "")
        from .runner import model_matches
        ok_models = [r["actual_model"] for r in _rows(
            conn, "SELECT DISTINCT actual_model FROM observations "
                  "WHERE status='ok' AND actual_model<>''")]
        add("actual model consistent with requested (ok rows)",
            all(model_matches(req, m) for m in ok_models)
            if ok_models else True,
            f"requested={req} actual={ok_models}")
        add("no sample rows in a live export",
            manifest.get("dataset_type") != "live"
            or all(d == dataset_id and d.startswith("live-") for d in
                   (dsids or [dataset_id])),
            f"dataset_type={manifest.get('dataset_type')} ids={dsids}")
        # response provenance + incomplete classification
        no_rid = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE status='ok' AND "
            "(response_id IS NULL OR response_id='')").fetchone()[0]
        add("ok rows carry response_id", no_rid == 0, f"{no_rid} missing")
        inc_ok = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE status='ok' AND "
            "response_status IS NOT NULL AND response_status NOT IN "
            "('', 'completed')").fetchone()[0]
        add("no incomplete responses counted as ok", inc_ok == 0,
            f"{inc_ok} incomplete-but-ok rows")
        # frozen library uniqueness
        pl = [p.get("prompt_id")
              for p in ds.dataset_prompts(study_id, dataset_id)]
        add("frozen prompt ids unique", len(pl) == len(set(pl)),
            f"{len(pl)} ids, {len(set(pl))} unique")
        el = [e.get("entity_id")
              for e in ds.dataset_entities(study_id, dataset_id)]
        add("frozen entity ids unique", len(el) == len(set(el)),
            f"{len(el)} ids, {len(set(el))} unique")
    finally:
        conn.close()
    return checks


def reconciliation_ok(checks) -> bool:
    return all(ok for _, ok, _ in checks)


# --------------------------------------------------------------------------- #
# Workbook
# --------------------------------------------------------------------------- #

def _write_sheet(wb, title, columns, rows, fills=None, flag_fn=None):
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    ws = wb.create_sheet(title[:31])
    hf = Font(bold=True, color="FFFFFF")
    hfill = PatternFill("solid", fgColor="481056")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(vertical="top", wrap_text=True)
    band = PatternFill("solid", fgColor="F4F0F7")
    for j, c in enumerate(columns, 1):
        cell = ws.cell(row=1, column=j, value=c)
        cell.font, cell.fill, cell.border = hf, hfill, border
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    for i, row in enumerate(rows):
        fill = flag_fn(row) if flag_fn else None
        if fill is None and i % 2 == 1:
            fill = band
        for j, c in enumerate(columns, 1):
            v = row.get(c, "")
            if isinstance(v, bool):
                v = "TRUE" if v else "FALSE"
            elif isinstance(v, str) and len(v) > 32000:
                v = v[:32000] + " ...[truncated - see raw JSON]"
            cell = ws.cell(row=2 + i, column=j, value=_formula_safe(v))
            cell.alignment, cell.border = wrap, border
            if fill is not None:
                cell.fill = fill
    for col_cells in ws.columns:
        letter = get_column_letter(col_cells[0].column)
        longest = max((min(len(str(c.value or "")), 80) for c in col_cells),
                      default=10)
        ws.column_dimensions[letter].width = max(10, min(60, longest + 2))
    return ws


def build_workbook(study_id: str, dataset_id: str, path: str) -> str:
    """Full study workbook (spec §11). Raises RuntimeError when
    reconciliation fails — a misleading workbook is never produced."""
    checks = reconcile(study_id, dataset_id)
    if not reconciliation_ok(checks):
        failed = [f"{n}: {d}" for n, ok, d in checks if not ok]
        raise RuntimeError("Reconciliation failed - " + " | ".join(failed))

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    manifest = ds.load_manifest(study_id, dataset_id)
    entities = ds.dataset_entities(study_id, dataset_id)
    prompts = ds.dataset_prompts(study_id, dataset_id)
    entity_domains = [e.get("primary_domain") for e in entities
                      if e.get("primary_domain")]
    conn = open_db(ds.dataset_db_path(study_id, dataset_id))
    try:
        wb = Workbook()
        ws = wb.active
        ws.title = "Overview"
        ws["A1"] = f'{manifest.get("study_name", "")} — Study Workbook'
        ws["A1"].font = Font(bold=True, size=14, color="481056")
        ov = analysis.overview(conn)
        info = [("Dataset", dataset_id),
                ("Dataset type", manifest.get("dataset_type", "").upper()),
                ("Study", manifest.get("study_name")),
                ("Study ID", study_id),
                ("Created", manifest.get("created_at")),
                ("Requested model", manifest.get("requested_model")),
                ("Actual model(s)",
                 ", ".join(manifest.get("actual_models", []))),
                ("Prompt-set version", manifest.get("prompt_set_version")),
                ("Prompt-set hash", manifest.get("prompt_set_hash")),
                ("Schema version", manifest.get("schema_version")),
                ("App version", manifest.get("app_version")),
                ("Git commit", manifest.get("git_commit")),
                ("Generated (UTC)", datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")),
                ("", "")] + [(k.replace("_", " "), v)
                             for k, v in ov.items()]
        for i, (k, v) in enumerate(info):
            ws.cell(row=3 + i, column=1, value=k).font = Font(bold=True)
            ws.cell(row=3 + i, column=2, value=v)
        ws.column_dimensions["A"].width = 34
        ws.column_dimensions["B"].width = 60
        if manifest.get("dataset_type") == "sample":
            c = ws.cell(row=2, column=1,
                        value="SAMPLE DATASET - not live research data")
            c.font = Font(bold=True, color="9C0006")
            c.fill = PatternFill("solid", fgColor="FFC7CE")

        _write_sheet(wb, "Study Manifest", ["key", "value"],
                     [{"key": k, "value": json.dumps(v, ensure_ascii=False)
                       if isinstance(v, (dict, list)) else v}
                      for k, v in manifest.items()])
        _write_sheet(wb, "Prompt Library",
                     ["prompt_id", "prompt_text", "category", "intent",
                      "location", "active", "notes"],
                     prompts)
        _write_sheet(wb, "Tracked Entities",
                     ["entity_id", "name", "primary_domain", "alt_domains",
                      "aliases", "entity_type", "role", "notes"],
                     [{**e, "alt_domains": "; ".join(e.get("alt_domains", [])),
                       "aliases": "; ".join(e.get("aliases", []))}
                      for e in entities])

        obs_cols = _ID_COLS + ["prompt_id", "run_number", "requested_model",
                               "actual_model", "response_id",
                               "response_status", "incomplete_reason",
                               "timestamp", "location",
                               "status", "error", "num_search_actions",
                               "num_queries", "num_queries_missing_text",
                               "num_site_queries", "num_open_page",
                               "num_find_in_page", "num_search_sources",
                               "num_citations", "num_unique_cited_urls",
                               "input_tokens", "output_tokens", "cost_usd",
                               "latency_ms", "prompt_text", "answer_text"]
        obs = _rows(conn, "SELECT * FROM observations ORDER BY prompt_id, "
                          "run_number")
        _write_sheet(wb, "Runs", obs_cols, obs)
        _write_sheet(wb, "Reasoning Summaries",
                     _ID_COLS + ["prompt_id", "run_number",
                                 "reasoning_summary"],
                     [o for o in obs if o.get("reasoning_summary")])
        _write_sheet(wb, "Search Trace",
                     ["observation_id", "dataset_id", "seq", "event_type",
                      "payload_json"],
                     _rows(conn, "SELECT * FROM trace_events "
                                 "ORDER BY observation_id, seq"))
        for sheet, table, order in [
                ("Fan-Out Queries", "fanout_queries", "observation_id, seq"),
                ("Search Sources", "search_sources", "observation_id, seq"),
                ("Opened Pages", "opened_pages", "observation_id, seq"),
                ("In-Page Searches", "inpage_searches", "observation_id, seq"),
                ("Citations", "citations", "observation_id"),
                ("Citation Occurrences", "citation_occurrences",
                 "observation_id, occurrence_number"),
                ("Search Calls", "search_calls",
                 "observation_id, sequence"),
                ("Source-Entity Links", "source_entity", "observation_id")]:
            rows = _rows(conn, f"SELECT * FROM {table} ORDER BY {order}")
            cols = list(rows[0].keys()) if rows else \
                [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
            _write_sheet(wb, sheet, cols, rows)

        _write_sheet(wb, "Entity Visibility",
                     ["entity_id", "entity", "role", "runs", "mention_rate",
                      "search_targeting_rate", "source_returned_rate",
                      "page_opened_rate", "page_searched_rate",
                      "citation_rate", "answer_mention_rate",
                      "prompts_appeared", "supporting_sources"],
                     analysis.entity_visibility(conn, entities))
        cov = analysis.prompt_coverage(conn, entities, prompts)
        _write_sheet(wb, "Prompt Coverage",
                     list(cov[0].keys()) if cov else ["prompt_id"], cov)
        gaps = analysis.citation_gaps(conn, entities)
        _write_sheet(wb, "Citation Gaps",
                     list(gaps[0].keys()) if gaps else ["prompt_id"], gaps)
        cl = analysis.query_clusters(conn, entities)
        _write_sheet(wb, "Query Clusters",
                     list(cl[0].keys()) if cl else ["cluster"], cl)

        flags = []
        for q in _rows(conn, "SELECT * FROM fanout_queries WHERE "
                             "has_site_operator=1 OR entity_hits<>'[]'"):
            kind = []
            if q["has_site_operator"]:
                kind.append("site:")
            if q["entity_hits"] and q["entity_hits"] != "[]":
                kind.append("entity-targeted")
            flags.append({"observation_id": q["observation_id"],
                          "dataset_id": q["dataset_id"],
                          "flag": "+".join(kind),
                          "detail": q["query"] or "(no query text)",
                          "site_domain": q["site_domain"]})
        _write_sheet(wb, "Flags",
                     ["observation_id", "dataset_id", "flag", "detail",
                      "site_domain"], flags)
        _write_sheet(wb, "Errors",
                     _ID_COLS + ["prompt_id", "run_number", "timestamp",
                                 "error"],
                     [o for o in obs if o.get("status") != "ok"])
        wb.save(path)
    finally:
        conn.close()
    return path


# --------------------------------------------------------------------------- #
# CSVs / ZIPs / templates
# --------------------------------------------------------------------------- #

def table_csv_bytes(study_id: str, dataset_id: str, table: str) -> bytes:
    conn = open_db(ds.dataset_db_path(study_id, dataset_id))
    try:
        rows = _rows(conn, f"SELECT * FROM {table} WHERE dataset_id=?",
                     (dataset_id,))
    finally:
        conn.close()
    buf = io.StringIO()
    cols = list(rows[0].keys()) if rows else []
    w = csv.DictWriter(buf, fieldnames=cols)
    if cols:
        w.writeheader()
        for r in rows:
            w.writerow({k: _formula_safe(r.get(k)) for k in cols})
    return buf.getvalue().encode("utf-8-sig")


def raw_zip_bytes(study_id: str, dataset_id: str) -> bytes:
    raw_dir = os.path.join(ds.dataset_dir(study_id, dataset_id), "raw")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.isdir(raw_dir):
            for name in sorted(os.listdir(raw_dir)):
                z.write(os.path.join(raw_dir, name), f"raw/{name}")
        man = os.path.join(ds.dataset_dir(study_id, dataset_id),
                           "manifest.json")
        if os.path.exists(man):
            z.write(man, "manifest.json")
    return buf.getvalue()


def data_package_bytes(study_id: str, dataset_id: str) -> bytes:
    """Workbook-ready package: manifest + prompts + entities + all table CSVs."""
    buf = io.BytesIO()
    ddir = ds.dataset_dir(study_id, dataset_id)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("manifest.json", "prompts.json", "entities.json"):
            p = os.path.join(ddir, f)
            if os.path.exists(p):
                z.write(p, f)
        for table in ("observations", "fanout_queries", "search_sources",
                      "opened_pages", "inpage_searches", "citations",
                      "source_entity", "entity_runs"):
            z.writestr(f"csv/{table}.csv",
                       table_csv_bytes(study_id, dataset_id, table))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Full dumps — every single detail as one JSON or one Markdown document
# --------------------------------------------------------------------------- #

_ALL_TABLES = ["observations", "trace_events", "fanout_queries",
               "search_sources", "opened_pages", "inpage_searches",
               "citations", "citation_occurrences", "search_calls",
               "source_entity", "entity_runs"]


def full_json_bytes(study_id: str, dataset_id: str) -> bytes:
    """One JSON document containing everything about the dataset: manifest,
    study config, frozen prompts/entities, every table row (including the
    ordered trace events), all analysis outputs and the reconciliation
    report. Raw API payloads stay in the raw ZIP (they are large); every
    extracted fact is here."""
    manifest = ds.load_manifest(study_id, dataset_id)
    entities = ds.dataset_entities(study_id, dataset_id)
    entity_domains = [e.get("primary_domain") for e in entities
                      if e.get("primary_domain")]
    conn = open_db(ds.dataset_db_path(study_id, dataset_id))
    try:
        doc = {
            "generated_utc": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "manifest": manifest,
            "study": (ds.load_study(study_id)
                      if os.path.exists(os.path.join(ds.study_dir(study_id),
                                                     "study.json")) else {}),
            "prompts": ds.dataset_prompts(study_id, dataset_id),
            "entities": entities,
            "tables": {t: _rows(conn, f"SELECT * FROM {t}")
                       for t in _ALL_TABLES},
            "analysis": {
                "overview": analysis.overview(conn),
                "retrieval_journey": analysis.retrieval_journey(conn),
                "domain_leaderboard": analysis.domain_leaderboard(
                    conn, entity_domains),
                "entity_visibility": analysis.entity_visibility(conn,
                                                                entities),
                "prompt_coverage": analysis.prompt_coverage(
                    conn, entities, ds.dataset_prompts(study_id, dataset_id)),
                "citation_gaps": analysis.citation_gaps(conn, entities),
                "query_clusters": analysis.query_clusters(conn, entities),
            },
            "reconciliation": [{"check": n, "ok": ok, "detail": d}
                               for n, ok, d in reconcile(study_id,
                                                         dataset_id)],
        }
    finally:
        conn.close()
    return json.dumps(doc, ensure_ascii=False, indent=2,
                      default=str).encode("utf-8")


def _md_table(rows, columns=None, limit=None) -> str:
    if not rows:
        return "_none_\n"
    columns = columns or list(rows[0].keys())
    def esc(v):
        s = "" if v is None else str(v)
        return s.replace("|", "\\|").replace("\n", " ")[:300]
    out = ["| " + " | ".join(columns) + " |",
           "|" + "|".join("---" for _ in columns) + "|"]
    for r in (rows[:limit] if limit else rows):
        out.append("| " + " | ".join(esc(r.get(c)) for c in columns) + " |")
    if limit and len(rows) > limit:
        out.append(f"\n_...{len(rows) - limit} more rows — see the JSON "
                   f"dump for the full table._")
    return "\n".join(out) + "\n"


def insights_markdown_bytes(study_id: str, dataset_id: str) -> bytes:
    """ONE readable Markdown file with all the insights from a dataset —
    headline metrics, entity visibility, prompt coverage, classified gaps
    with recommendations, clusters, domain leaderboard and notable cited
    claims — without the per-run trace dumps (those live in the full
    report)."""
    manifest = ds.load_manifest(study_id, dataset_id)
    entities = ds.dataset_entities(study_id, dataset_id)
    entity_domains = [e.get("primary_domain") for e in entities
                      if e.get("primary_domain")]
    primary_ids = [e["entity_id"] for e in entities
                   if e.get("role") == "primary"]
    conn = open_db(ds.dataset_db_path(study_id, dataset_id))
    L = []
    try:
        t = manifest.get("dataset_type", "?").upper()
        tot = manifest.get("totals", {})
        L.append(f"# {manifest.get('study_name', study_id)} - insights\n")
        if manifest.get("dataset_type") == "sample":
            L.append("> **SAMPLE DATASET - fabricated fixtures, not live "
                     "research data.**\n")
        L.append(f"**{t}** `{dataset_id}` | requested "
                 f"**{manifest.get('requested_model')}** | actual "
                 f"**{', '.join(manifest.get('actual_models', [])) or '-'}**"
                 f" | prompt-set {manifest.get('prompt_set_version')} "
                 f"({manifest.get('prompt_set_hash')}) | ok "
                 f"{tot.get('ok', 0)} / failed {tot.get('failed', 0)} | "
                 f"cost ${tot.get('cost_usd', 0)} | "
                 f"{manifest.get('created_at', '')}\n")
        recon = reconcile(study_id, dataset_id)
        bad = [f"{n}: {d}" for n, ok, d in recon if not ok]
        L.append(("WARNING - reconciliation failed: " + "; ".join(bad)
                  if bad else "All reconciliation checks passed.") + "\n")

        ov = analysis.overview(conn)
        L.append("\n## Key numbers\n")
        L.append(_md_table([{"metric": k.replace("_", " "), "value": v}
                            for k, v in ov.items()]))

        if entities:
            L.append("\n## Entity visibility (rates = share of runs)\n")
            L.append(_md_table(analysis.entity_visibility(conn, entities)))

        L.append("\n## Prompt coverage\n")
        L.append(_md_table(analysis.prompt_coverage(
            conn, entities, ds.dataset_prompts(study_id, dataset_id))))

        gaps = analysis.citation_gaps(conn, entities)
        if gaps:
            L.append("\n## Citation gaps - classified recommendations\n")
            by_prompt = {}
            for g in gaps:
                by_prompt.setdefault(
                    (g["prompt_id"], g["prompt_text"],
                     g["primary_cited_runs"], g["runs"]), []).append(g)
            for (pid, ptext, c, r), items in by_prompt.items():
                L.append(f"\n### {pid} - cited {c}/{r} runs\n")
                L.append(f"_{ptext}_\n")
                for g in items:
                    L.append(f"- **{g['gap_type']}** - {g['evidence']}")
                    if g.get("supporting_queries"):
                        L.append(f"  - queries: {g['supporting_queries']}")
                    if g.get("winning_domains"):
                        L.append(f"  - winning sources: "
                                 f"{g['winning_domains']}")

        L.append("\n## Query clusters (stable taxonomy)\n")
        L.append(_md_table(analysis.query_clusters(conn, entities)))

        L.append("\n## Domain leaderboard (one row per canonical domain)\n")
        L.append(_md_table(analysis.domain_leaderboard(conn, entity_domains),
                           limit=25))

        if primary_ids:
            occ = _rows(conn, """SELECT co.answer_sentence, co.raw_url,
                o.prompt_id FROM citation_occurrences co
                JOIN observations o USING(observation_id)
                WHERE co.answer_sentence <> ''""")
            from .entities import EntityMatcher
            m = EntityMatcher(entities)
            notable = []
            for r in occ:
                if any(eid in primary_ids
                       for eid in m.match_domain(
                           r["raw_url"].split("/")[2].replace("www.", "")
                           if "://" in r["raw_url"] else "")):
                    notable.append(r)
            if notable:
                L.append("\n## Notable cited claims (primary entity)\n")
                for r in notable[:10]:
                    L.append(f"- ({r['prompt_id']}) \"{r['answer_sentence']}\" "
                             f"- {r['raw_url']}")

        L.append("\n---\n_Rates across repeat runs; a single answer is a "
                 "sample. Reasoning summaries, where any, are model-generated "
                 "- not hidden chain of thought. Full per-run traces: the "
                 "full study report._\n")
    finally:
        conn.close()
    return "\n".join(L).encode("utf-8")


def full_markdown_bytes(study_id: str, dataset_id: str) -> bytes:
    """A complete human-readable report: identity, metrics, every analysis
    table, then EVERY run's chronological trace with queries, sources,
    opens, in-page searches, citations, entity flags and the full answer."""
    manifest = ds.load_manifest(study_id, dataset_id)
    entities = ds.dataset_entities(study_id, dataset_id)
    ent_names = {e["entity_id"]: e["name"] for e in entities}
    entity_domains = [e.get("primary_domain") for e in entities
                      if e.get("primary_domain")]
    conn = open_db(ds.dataset_db_path(study_id, dataset_id))
    L = []
    try:
        t = manifest.get("dataset_type", "?").upper()
        tot = manifest.get("totals", {})
        L.append(f"# {manifest.get('study_name', study_id)} — full study "
                 f"report\n")
        if manifest.get("dataset_type") == "sample":
            L.append("> **SAMPLE DATASET — fabricated fixtures, not live "
                     "research data.**\n")
        L.append(f"**{t} dataset** `{dataset_id}` · study `{study_id}` · "
                 f"created {manifest.get('created_at')} · requested model "
                 f"**{manifest.get('requested_model')}** · actual "
                 f"**{', '.join(manifest.get('actual_models', [])) or '—'}**"
                 f" · {manifest.get('search_mode')} · prompt-set "
                 f"{manifest.get('prompt_set_version')} "
                 f"(hash {manifest.get('prompt_set_hash')}) · schema "
                 f"{manifest.get('schema_version')} · app "
                 f"{manifest.get('app_version')} "
                 f"({manifest.get('git_commit')}) · ok {tot.get('ok', 0)} / "
                 f"failed {tot.get('failed', 0)} · cost "
                 f"${tot.get('cost_usd', 0)}\n")
        recon = reconcile(study_id, dataset_id)
        bad = [f"{n}: {d}" for n, ok, d in recon if not ok]
        L.append(("⚠️ **Reconciliation FAILED:** " + "; ".join(bad)
                  if bad else "✅ All reconciliation checks passed.") + "\n")

        L.append("## Overview\n")
        L.append(_md_table([{"metric": k.replace("_", " "), "value": v}
                            for k, v in analysis.overview(conn).items()]))
        L.append("\n## Retrieval journey (observable stages — not a strict "
                 "funnel)\n")
        L.append(_md_table([{"stage": a, "count": b}
                            for a, b in analysis.retrieval_journey(conn)]))
        L.append("\n## Tracked entities\n")
        L.append(_md_table([{**e, "alt_domains": "; ".join(
            e.get("alt_domains", [])), "aliases": "; ".join(
            e.get("aliases", []))} for e in entities]))
        L.append("\n## Entity visibility\n")
        L.append(_md_table(analysis.entity_visibility(conn, entities)))
        L.append("\n## Prompt coverage\n")
        L.append(_md_table(analysis.prompt_coverage(
            conn, entities, ds.dataset_prompts(study_id, dataset_id))))
        L.append("\n## Citation gaps (classified, evidence-linked)\n")
        L.append(_md_table(analysis.citation_gaps(conn, entities)))
        L.append("\n## Query clusters (stable taxonomy)\n")
        L.append(_md_table(analysis.query_clusters(conn, entities)))
        L.append("\n## Domain leaderboard (one row per canonical domain)\n")
        L.append(_md_table(analysis.domain_leaderboard(conn, entity_domains)))

        L.append("\n---\n\n# Per-run Search & Evidence Traces\n")
        L.append("_Reasoning summaries, where present, are model-generated "
                 "summaries — not the model's hidden raw chain of thought. "
                 "Search actions without query text are shown as such, never "
                 "inferred._\n")
        obs = _rows(conn, "SELECT * FROM observations "
                          "ORDER BY prompt_id, run_number")
        for o in obs:
            oid = o["observation_id"]
            L.append(f"\n## {o['prompt_id']} · run {o['run_number']} "
                     f"({o['status']})\n")
            L.append(f"`{oid}` · {o['timestamp']} · {o['location']} · "
                     f"requested {o['requested_model']} · actual "
                     f"{o['actual_model'] or '—'} · tokens "
                     f"{o.get('input_tokens') or 0}/"
                     f"{o.get('output_tokens') or 0} (reasoning "
                     f"{o.get('reasoning_tokens') or 0}) · latency "
                     f"{o.get('latency_ms') or 0} ms · cost "
                     f"${o.get('cost_usd') or 0}\n")
            L.append(f"\n**Prompt:** {o['prompt_text']}\n")
            if o.get("status") != "ok":
                L.append(f"\n**Error:** `{o.get('error')}`\n")
                continue
            if o.get("reasoning_summary"):
                L.append(f"\n**Reasoning summary** (model-generated, not "
                         f"hidden chain of thought):\n> "
                         + o["reasoning_summary"].replace("\n", "\n> ")
                         + "\n")
            else:
                L.append("\n_No reasoning summary returned._\n")

            queries = _rows(conn, "SELECT * FROM fanout_queries WHERE "
                                  "observation_id=? ORDER BY seq", (oid,))
            q_by_seq = {}
            for q in queries:
                q_by_seq.setdefault(q["seq"], []).append(q)
            srcs = _rows(conn, "SELECT * FROM search_sources WHERE "
                               "observation_id=? ORDER BY seq", (oid,))
            src_by_action = {}
            for x in srcs:
                src_by_action.setdefault(x["action_seq"], []).append(x)
            events = _rows(conn, "SELECT * FROM trace_events WHERE "
                                 "observation_id=? ORDER BY seq", (oid,))
            L.append("\n**Retrieval trail (chronological):**\n")
            step = 0
            for e in events:
                et = e["event_type"]
                payload = json.loads(e["payload_json"] or "{}")
                if et == "search":
                    step += 1
                    qs = q_by_seq.get(e["seq"], [])
                    L.append(f"{step}. 🔎 Search batch "
                             f"{(qs[0]['batch_index'] + 1) if qs else '?'}")
                    for q in qs:
                        if q["query_missing"]:
                            L.append("   - _Search action returned without "
                                     "query text._")
                        else:
                            tag = (f" — `site:{q['site_domain']}`"
                                   if q["has_site_operator"] else "")
                            hits = json.loads(q["entity_hits"] or "[]")
                            if hits:
                                tag += " — targets " + ", ".join(
                                    ent_names.get(h, h) for h in hits)
                            L.append(f"   - `{q['query']}`{tag} "
                                     f"[{q['taxonomy']}]")
                    for sx in src_by_action.get(e["seq"], []):
                        L.append(f"   - ↳ returned: {sx['raw_url']}"
                                 + (f" ({sx['title']})" if sx["title"]
                                    else ""))
                elif et == "open_page":
                    step += 1
                    L.append(f"{step}. 🌐 Opened page: {payload.get('url')}")
                elif et == "find_in_page":
                    step += 1
                    L.append(f"{step}. 🔬 In-page search for "
                             f"`{payload.get('pattern')}` in "
                             f"{payload.get('url')}")
            cits = _rows(conn, "SELECT * FROM citations WHERE "
                               "observation_id=? ORDER BY citation_count "
                               "DESC", (oid,))
            L.append("\n**Cited in the final answer:**\n")
            if cits:
                for c in cits:
                    L.append(f"- {c['raw_url']} — {c['title'] or ''} "
                             f"(×{c['citation_count']}; canonical: "
                             f"{c['canonical_url']})")
            else:
                L.append("- _none_")
            ers = _rows(conn, "SELECT * FROM entity_runs WHERE "
                              "observation_id=?", (oid,))
            if ers:
                L.append("\n**Entities in this run:**\n")
                L.append(_md_table([{
                    "entity": ent_names.get(x["entity_id"], x["entity_id"]),
                    "searched": bool(x["search_targeted"]),
                    "source returned": bool(x["source_returned"]),
                    "page opened": bool(x["page_opened"]),
                    "page searched": bool(x["page_searched"]),
                    "cited": bool(x["cited"]),
                    "named in answer": bool(x["named_in_answer"])}
                    for x in ers]))
            L.append("\n**Complete final answer:**\n")
            L.append("> " + (o.get("answer_text") or "_empty_")
                     .replace("\n", "\n> ") + "\n")
    finally:
        conn.close()
    return "\n".join(L).encode("utf-8")


PROMPT_TEMPLATE_COLUMNS = ["prompt_id", "prompt_text", "category", "intent",
                           "location", "active", "notes"]


def prompt_template_bytes(fmt: str = "xlsx") -> bytes:
    example = [
        {"prompt_id": "P01",
         "prompt_text": "Who are the best SEO agencies in Melbourne?",
         "category": "discovery", "intent": "commercial",
         "location": "", "active": "TRUE", "notes": ""},
        {"prompt_id": "P02",
         "prompt_text": "Recommend a technical SEO consultant in Melbourne.",
         "category": "technical", "intent": "commercial",
         "location": "Melbourne, Victoria, AU", "active": "TRUE",
         "notes": "location override example"},
    ]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=PROMPT_TEMPLATE_COLUMNS)
        w.writeheader()
        for r in example:
            w.writerow(r)
        return buf.getvalue().encode("utf-8-sig")
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "prompts"
    ws.append(PROMPT_TEMPLATE_COLUMNS)
    for r in example:
        ws.append([r[c] for c in PROMPT_TEMPLATE_COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
