"""Read-side analysis over ONE dataset db: overview metrics, retrieval
journey, prompt coverage, entity visibility, domain leaderboard, citation
gaps, query clusters. Everything here takes an open sqlite connection to a
single dataset's observations.db — cross-dataset math is impossible by
construction.
"""

from __future__ import annotations

import json
from collections import Counter

from .classify import classify_domain, diagnose_gap, TAXONOMY
from .entities import EntityMatcher


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return list(r) if r else []


def overview(conn) -> dict:
    n, ok = (_one(conn, "SELECT COUNT(*), SUM(status='ok') FROM observations")
             or [0, 0])
    ok = ok or 0
    searched = _one(conn, "SELECT SUM(num_search_actions>0) FROM observations "
                          "WHERE status='ok'")[0] or 0
    sums = _one(conn, """SELECT SUM(num_queries), SUM(num_site_queries),
        SUM(num_open_page), SUM(num_find_in_page), SUM(num_search_sources),
        SUM(num_citations), SUM(COALESCE(cost_usd,0)),
        SUM(num_queries_missing_text)
        FROM observations WHERE status='ok'""")
    qn, siten, openn, findn, srcn, citn, cost, qmiss = [s or 0 for s in sums]
    udom = _one(conn, "SELECT COUNT(DISTINCT domain) FROM search_sources")[0] or 0
    ucited = _one(conn, "SELECT COUNT(DISTINCT canonical_url) FROM citations")[0] or 0
    ent = _one(conn, """SELECT AVG(named_in_answer), AVG(cited),
        COUNT(DISTINCT entity_id) FROM entity_runs""") or [None, None, 0]
    return {
        "observations": n or 0, "ok": ok, "failed": (n or 0) - ok,
        "search_trigger_rate": (searched / ok) if ok else 0,
        "avg_fanout_per_run": (qn / ok) if ok else 0,
        "site_share": (siten / qn) if qn else 0,
        "queries_missing_text": qmiss,
        "unique_source_domains": udom,
        "pages_opened": openn, "inpage_searches": findn,
        "search_sources": srcn,
        "total_citations": citn, "unique_cited_urls": ucited,
        "entity_mention_rate": ent[0], "entity_citation_rate": ent[1],
        "entities_tracked": ent[2] or 0,
        "cost_usd": round(cost, 4),
    }


def retrieval_journey(conn) -> list:
    """Observable stage volumes — NOT a strict conversion funnel."""
    o = overview(conn)
    stages = [
        ("Fan-out queries", int(o["avg_fanout_per_run"] * o["ok"])),
        ("site: probes", _one(conn, "SELECT SUM(num_site_queries) FROM "
                                    "observations WHERE status='ok'")[0] or 0),
        ("Source URLs returned", o["search_sources"]),
        ("Pages opened", o["pages_opened"]),
        ("In-page searches", o["inpage_searches"]),
        ("URLs cited", o["total_citations"]),
    ]
    ent_cited = _one(conn, "SELECT SUM(cited) FROM entity_runs")[0]
    ent_named = _one(conn, "SELECT SUM(named_in_answer) FROM entity_runs")[0]
    if ent_cited is not None:
        stages.append(("Tracked-entity citations (runs)", ent_cited or 0))
        stages.append(("Tracked entities named in answer (runs)",
                       ent_named or 0))
    return stages


def domain_leaderboard(conn, entity_domains) -> list:
    """ONE aggregated row per canonical domain. Classification is a property
    of the domain; entity attribution lives in source_entity, summarised in
    its own column here without duplicating rows."""
    rows = {}
    for r in _rows(conn, """SELECT domain, COUNT(*) n FROM search_sources
                            GROUP BY domain"""):
        rows.setdefault(r["domain"], {}).update(returned=r["n"])
    for r in _rows(conn, """SELECT domain, COUNT(*) n FROM opened_pages
                            GROUP BY domain"""):
        rows.setdefault(r["domain"], {}).update(opened=r["n"])
    for r in _rows(conn, """SELECT domain, SUM(citation_count) n,
                            COUNT(DISTINCT canonical_url) u,
                            COUNT(DISTINCT observation_id) o
                            FROM citations GROUP BY domain"""):
        rows.setdefault(r["domain"], {}).update(
            citations=r["n"], unique_cited_urls=r["u"], runs_citing=r["o"])
    ent_by_domain = {}
    for r in _rows(conn, """SELECT domain, entity_id, relation,
                            COUNT(*) n FROM source_entity
                            GROUP BY domain, entity_id, relation"""):
        ent_by_domain.setdefault(r["domain"], []).append(
            f'{r["entity_id"]}:{r["relation"]}')
    out = []
    for dom, d in rows.items():
        out.append({
            "domain": dom,
            "class": classify_domain(dom, entity_domains),
            "returned_in_search": d.get("returned", 0),
            "pages_opened": d.get("opened", 0),
            "citations": d.get("citations", 0),
            "unique_cited_urls": d.get("unique_cited_urls", 0),
            "runs_citing": d.get("runs_citing", 0),
            "entity_links": "; ".join(ent_by_domain.get(dom, [])),
        })
    out.sort(key=lambda r: (-r["citations"], -r["returned_in_search"]))
    return out


def entity_visibility(conn, entities) -> list:
    """One row per tracked entity (spec §2)."""
    ok = _one(conn, "SELECT COUNT(*) FROM observations WHERE status='ok'")[0] or 0
    out = []
    by_id = {e["entity_id"]: e for e in entities}
    for r in _rows(conn, """SELECT entity_id,
            SUM(mentioned_anywhere) m_any, SUM(named_in_answer) named,
            SUM(search_targeted) st,
            SUM(source_returned) sr, SUM(page_opened) po,
            SUM(page_searched) ps,
            SUM(cited) c, COUNT(*) n FROM entity_runs GROUP BY entity_id"""):
        e = by_id.get(r["entity_id"], {})
        prompts = [x["prompt_id"] for x in _rows(
            conn, """SELECT DISTINCT o.prompt_id FROM entity_runs er
                     JOIN observations o USING(observation_id)
                     WHERE er.entity_id=? AND
                     (er.named_in_answer=1 OR er.cited=1)""",
            (r["entity_id"],))]
        srcs = [x["canonical_url"] for x in _rows(
            conn, """SELECT DISTINCT canonical_url FROM source_entity
                     WHERE entity_id=? LIMIT 12""", (r["entity_id"],))]
        n = max(r["n"], 1)
        out.append({
            "entity_id": r["entity_id"],
            "entity": e.get("name", r["entity_id"]),
            "role": e.get("role", ""),
            "runs": r["n"],
            "mention_rate": round((r["m_any"] or 0) / n, 3),
            "search_targeting_rate": round((r["st"] or 0) / n, 3),
            "source_returned_rate": round((r["sr"] or 0) / n, 3),
            "page_opened_rate": round((r["po"] or 0) / n, 3),
            "page_searched_rate": round((r["ps"] or 0) / n, 3),
            "citation_rate": round((r["c"] or 0) / n, 3),
            # "Answer mention", not "recommendation": named in the final
            # answer. Actual recommendation detection is not implemented, so
            # the metric is not called that.
            "answer_mention_rate": round((r["named"] or 0) / n, 3),
            "prompts_appeared": "; ".join(sorted(set(p for p in prompts if p))),
            "supporting_sources": "; ".join(srcs),
        })
    out.sort(key=lambda r: (-r["citation_rate"], -r["answer_mention_rate"]))
    return out


def prompt_coverage(conn, entities, prompt_meta=None) -> list:
    """The primary analysis table (spec §8)."""
    prompt_meta = {p.get("prompt_id"): p for p in (prompt_meta or [])}
    primary_ids = [e["entity_id"] for e in entities
                   if e.get("role") == "primary"]
    out = []
    for r in _rows(conn, """SELECT prompt_id, MIN(prompt_text) ptext,
            COUNT(*) runs, SUM(num_search_actions>0) searched,
            AVG(num_queries) avg_q,
            CAST(SUM(num_site_queries) AS REAL)/MAX(SUM(num_queries),1) site_r
            FROM observations WHERE status='ok' GROUP BY prompt_id"""):
        pid = r["prompt_id"]
        runs = max(r["runs"], 1)
        ent = {"mention": 0, "cite": 0, "named": 0}
        if primary_ids:
            e1 = _one(conn, """SELECT SUM(er.mentioned_anywhere),
                SUM(er.cited), SUM(er.named_in_answer)
                FROM entity_runs er JOIN observations o USING(observation_id)
                WHERE o.prompt_id=? AND er.entity_id IN (%s)"""
                % ",".join("?" * len(primary_ids)),
                [pid] + primary_ids)
            ent = {"mention": e1[0] or 0, "cite": e1[1] or 0,
                   "named": e1[2] or 0}
        # top competing entity = non-primary with most cited runs on prompt
        top_comp = ""
        comp = _rows(conn, """SELECT er.entity_id, SUM(er.cited) c,
            SUM(er.named_in_answer) nm FROM entity_runs er
            JOIN observations o USING(observation_id)
            WHERE o.prompt_id=? GROUP BY er.entity_id
            ORDER BY c DESC, nm DESC""", (pid,))
        by_id = {e["entity_id"]: e for e in entities}
        for c in comp:
            e = by_id.get(c["entity_id"], {})
            if e.get("role") != "primary" and ((c["c"] or 0) + (c["nm"] or 0)):
                top_comp = f'{e.get("name", c["entity_id"])} ' \
                           f'({c["c"] or 0}/{runs} cited)'
                break
        doms = _rows(conn, """SELECT c.domain, SUM(c.citation_count) n
            FROM citations c JOIN observations o USING(observation_id)
            WHERE o.prompt_id=? GROUP BY c.domain ORDER BY n DESC LIMIT 4""",
            (pid,))
        meta = prompt_meta.get(pid, {})
        row = {
            "prompt_id": pid, "prompt_text": r["ptext"],
            "category": meta.get("category", ""),
            "intent": meta.get("intent", ""),
            "runs": r["runs"],
            "search_trigger_rate": round((r["searched"] or 0) / runs, 2),
            "avg_fanouts": round(r["avg_q"] or 0, 1),
            "site_rate": round(r["site_r"] or 0, 2),
            "entity_mention_rate": round(ent["mention"] / runs, 2)
                                   if primary_ids else None,
            "entity_citation_rate": round(ent["cite"] / runs, 2)
                                    if primary_ids else None,
            "entity_answer_mention_rate": round(ent["named"] / runs, 2)
                                        if primary_ids else None,
            "top_competing_entity": top_comp,
            "dominant_source_domains": "; ".join(
                f'{d["domain"]} ({d["n"]})' for d in doms),
        }
        out.append(row)
    out.sort(key=lambda r: r["prompt_id"] or "")
    return out


def citation_gaps(conn, entities) -> list:
    """Per-prompt gap diagnosis for the primary entity, evidence-linked."""
    primary = [e for e in entities if e.get("role") == "primary"]
    if not primary:
        return []
    p = primary[0]
    pid_ = p["entity_id"]
    entity_domains = [e.get("primary_domain") for e in entities
                      if e.get("primary_domain")]
    out = []
    for row in prompt_coverage(conn, entities):
        pid = row["prompt_id"]
        runs = row["runs"]
        cited_runs = int(round((row["entity_citation_rate"] or 0) * runs))
        if cited_runs >= runs and runs > 0:
            continue  # no gap on this prompt
        squeries = [q["query"] for q in _rows(conn, """SELECT q.query FROM
            fanout_queries q JOIN observations o USING(observation_id)
            WHERE o.prompt_id=? AND q.entity_hits LIKE ?""",
            (pid, f'%{pid_}%')) if q["query"]]
        # "Retrieved" in the gap diagnosis means RETURNED BY SEARCH — a
        # distinct stage from opened/cited (evidence_stage on the bridge).
        fp_urls = [s["canonical_url"] for s in _rows(conn, """SELECT DISTINCT
            se.canonical_url FROM source_entity se
            JOIN observations o USING(observation_id)
            WHERE o.prompt_id=? AND se.entity_id=? AND
            se.relation='first_party' AND
            se.evidence_stage='search_source'""", (pid, pid_))]
        fp_any_stage = [s["canonical_url"] for s in _rows(conn, """SELECT
            DISTINCT se.canonical_url FROM source_entity se
            JOIN observations o USING(observation_id)
            WHERE o.prompt_id=? AND se.entity_id=? AND
            se.relation='first_party'""", (pid, pid_))]
        winners = [(d["domain"], classify_domain(d["domain"], entity_domains),
                    d["n"]) for d in _rows(conn, """SELECT c.domain,
            SUM(c.citation_count) n FROM citations c
            JOIN observations o USING(observation_id)
            WHERE o.prompt_id=? GROUP BY c.domain ORDER BY n DESC LIMIT 6""",
            (pid,))]
        tax = Counter(t["taxonomy"] for t in _rows(conn, """SELECT q.taxonomy
            FROM fanout_queries q JOIN observations o USING(observation_id)
            WHERE o.prompt_id=? AND q.query_missing=0""", (pid,)))
        diags = diagnose_gap({
            "runs": runs, "primary_cited_runs": cited_runs,
            "primary_searched": bool(squeries),
            "primary_search_queries": squeries,
            "primary_source_urls": fp_urls,          # returned by search
            "primary_any_stage_urls": fp_any_stage,  # any retrieval stage
            "primary_cited": cited_runs > 0,
            "winner_domains": winners, "query_taxonomies": tax,
        })
        for key, label, evidence in diags:
            out.append({
                "prompt_id": pid, "prompt_text": row["prompt_text"],
                "primary_cited_runs": cited_runs, "runs": runs,
                "gap_type": label, "gap_key": key,
                "evidence": evidence,
                "supporting_queries": " | ".join(squeries[:4]),
                "winning_domains": "; ".join(
                    f"{d} [{c}] {n}" for d, c, n in winners[:4]),
            })
    return out


def query_clusters(conn, entities) -> list:
    """Stable 15-category taxonomy rollup (spec §9), with per-category
    example queries and cited-domain association."""
    matcher = EntityMatcher(entities) if entities else None
    rows = _rows(conn, """SELECT q.taxonomy, q.query, q.has_site_operator,
        q.entity_hits, q.observation_id FROM fanout_queries q
        WHERE q.query_missing=0""")
    cited_by_obs = {}
    for r in _rows(conn, "SELECT observation_id, domain FROM citations"):
        cited_by_obs.setdefault(r["observation_id"], set()).add(r["domain"])
    agg = {}
    for r in rows:
        a = agg.setdefault(r["taxonomy"], {
            "queries": 0, "site": 0, "entity_q": 0,
            "examples": [], "obs": set(), "domains": Counter()})
        a["queries"] += 1
        a["site"] += r["has_site_operator"] or 0
        if r["entity_hits"] and r["entity_hits"] != "[]":
            a["entity_q"] += 1
        if len(a["examples"]) < 4 and r["query"] not in a["examples"]:
            a["examples"].append(r["query"])
        a["obs"].add(r["observation_id"])
        for d in cited_by_obs.get(r["observation_id"], ()):
            a["domains"][d] += 1
    out = []
    for tax in TAXONOMY:
        if tax not in agg:
            continue
        a = agg[tax]
        out.append({
            "cluster": tax, "queries": a["queries"],
            "site_probes": a["site"], "entity_targeted": a["entity_q"],
            "runs_touched": len(a["obs"]),
            "co_cited_domains": "; ".join(
                f"{d} ({n})" for d, n in a["domains"].most_common(5)),
            "example_queries": " | ".join(a["examples"]),
        })
    out.sort(key=lambda r: -r["queries"])
    return out
