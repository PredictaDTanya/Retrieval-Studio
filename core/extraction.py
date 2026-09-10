"""Parse one raw Responses API payload into an ordered trace + typed rows.

Distinct concepts, never conflated (spec §6):
    fanout_queries   the queries a search action ran (query text can be
                     MISSING — recorded as query_missing=1, never invented)
    search_sources   URLs returned by search actions
                     (web_search_call.action.sources)
    opened_pages     open_page actions
    inpage_searches  find_in_page actions (+ pattern)
    citations        url_citation annotations on the final answer

Reasoning summaries: taken ONLY from `reasoning`-type output items with
summary content. Never fabricated; absent -> None.

Everything preserves output order via `seq` (position in the output array,
then intra-item order).
"""

from __future__ import annotations

import json

from .model import SITE_OP_RE, canonical_url, domain_of


def extract_usage(raw: dict) -> dict:
    usage = raw.get("usage") or {}
    outd = usage.get("output_tokens_details") or {}
    ind = usage.get("input_tokens_details") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "reasoning_tokens": outd.get("reasoning_tokens"),
        "cached_tokens": ind.get("cached_tokens"),
    }


def compute_cost(model: str, usage: dict, pricing: dict,
                 num_web_search_calls: int = 0):
    rate = (pricing or {}).get(model)
    if not isinstance(rate, dict):
        return None
    it, ot = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
    ct = usage.get("cached_tokens") or 0
    in_rate = float(rate.get("input_per_mtok", 0))
    cached_rate = float(rate.get("cached_input_per_mtok", in_rate))
    cost = (max(it - ct, 0) / 1e6) * in_rate + (ct / 1e6) * cached_rate \
        + (ot / 1e6) * float(rate.get("output_per_mtok", 0)) \
        + num_web_search_calls * float(rate.get("per_web_search_call", 0))
    return round(cost, 6)


def _sentence_around(text: str, idx: int) -> str:
    """The sentence containing char idx (bounded by . ! ? or newline)."""
    if not text:
        return ""
    idx = max(0, min(idx, len(text) - 1))
    start = max(text.rfind(ch, 0, idx) for ch in ".!?\n")
    start = 0 if start < 0 else start + 1
    ends = [text.find(ch, idx) for ch in ".!?\n"]
    ends = [e for e in ends if e != -1]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end].strip()[:400]


def _iter_source_urls(container: dict):
    """Yield (url, title, source_type). OpenAI can return non-URL feed
    sources (type oai-weather / oai-sports / oai-finance) - preserved with
    their type rather than reduced to nothing."""
    for key in ("sources", "results"):
        items = container.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, str):
                yield it, "", "url"
            elif isinstance(it, dict):
                url = it.get("url") or it.get("link") or ""
                stype = it.get("type") or ("url" if url else "")
                if url or stype:
                    yield (url, it.get("title") or it.get("name") or "",
                           stype)


def extract_observation(raw: dict, matcher=None) -> dict:
    """Returns a dict of typed row-lists + rollups + answer + reasoning.

    matcher: entities.EntityMatcher or None (unbranded study).
    Keys: events, fanout_queries, search_sources, opened_pages,
          inpage_searches, citations, source_entity, entity_runs (partial —
          per-entity booleans), answer_text, reasoning_summary, rollups,
          actual_model, usage.
    """
    output = raw.get("output") or []
    events, queries, sources, opened, inpage, cite_map = [], [], [], [], [], {}
    search_calls, occurrences = [], []
    answer_parts, reasoning_parts = [], []
    seq = 0
    batch_index = -1
    n_search = 0
    answer_offset = 0  # running char offset of joined answer blocks

    def ev(event_type, payload):
        nonlocal seq
        events.append({"seq": seq, "event_type": event_type,
                       "payload_json": json.dumps(payload, ensure_ascii=False,
                                                  default=str)[:8000]})
        seq += 1
        return seq - 1

    for item in output:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")

        if itype == "reasoning":
            # Summary text lives in item["summary"] as [{type, text}] parts.
            parts = item.get("summary") or []
            texts = [p.get("text", "") for p in parts
                     if isinstance(p, dict) and p.get("text")]
            if texts:
                reasoning_parts.extend(texts)
                ev("reasoning", {"summary": " ".join(texts)[:4000]})

        elif itype == "web_search_call":
            action = item.get("action") or {}
            atype = action.get("type") or "search"

            call_sources = (list(_iter_source_urls(action))
                            + list(_iter_source_urls(item)))
            if atype == "search":
                n_search += 1
                batch_index += 1
                qlist = action.get("queries")
                if not qlist:
                    q = action.get("query")
                    qlist = [q] if q else []
                if isinstance(qlist, str):
                    qlist = [qlist]
                qlist = [q for q in qlist if q]
                action_seq = ev("search", {
                    "batch": batch_index, "queries": qlist,
                    "n_sources": len(call_sources)})
                if qlist:
                    for q in qlist:
                        m = SITE_OP_RE.search(q)
                        sd = domain_of(m.group(1)) if m else ""
                        hits = matcher.match_text(q) if matcher else []
                        if not hits and sd and matcher:
                            hits = matcher.match_domain(sd)
                        queries.append({
                            "seq": action_seq, "batch_index": batch_index,
                            "query": q, "query_missing": 0,
                            "has_site_operator": 1 if m else 0,
                            "site_domain": sd,
                            "entity_hits": json.dumps(hits),
                        })
                else:
                    # Search ran but the API returned no query text.
                    queries.append({
                        "seq": action_seq, "batch_index": batch_index,
                        "query": None, "query_missing": 1,
                        "has_site_operator": 0, "site_domain": "",
                        "entity_hits": "[]",
                    })
                for url, title, stype in call_sources:
                    sources.append({
                        "seq": seq, "action_seq": action_seq,
                        "raw_url": url, "canonical_url": canonical_url(url),
                        "domain": domain_of(url), "title": title,
                        "source_type": stype,
                    })
                search_calls.append({
                    "sequence": action_seq,
                    "search_call_id": item.get("id") or "",
                    "search_call_status": item.get("status") or "",
                    "action_type": "search",
                    "query_count": len(qlist),
                    "source_count": len(call_sources)})

            elif atype in ("open_page", "open"):
                url = action.get("url") or ""
                ev("open_page", {"url": url})
                opened.append({"seq": seq - 1, "raw_url": url,
                               "canonical_url": canonical_url(url),
                               "domain": domain_of(url)})
                search_calls.append({
                    "sequence": seq - 1,
                    "search_call_id": item.get("id") or "",
                    "search_call_status": item.get("status") or "",
                    "action_type": "open_page",
                    "query_count": 0, "source_count": len(call_sources)})

            elif atype == "find_in_page":
                url = action.get("url") or ""
                pattern = action.get("pattern") or action.get("query") or ""
                ev("find_in_page", {"url": url, "pattern": pattern})
                inpage.append({"seq": seq - 1, "raw_url": url,
                               "canonical_url": canonical_url(url),
                               "domain": domain_of(url), "pattern": pattern})
                search_calls.append({
                    "sequence": seq - 1,
                    "search_call_id": item.get("id") or "",
                    "search_call_status": item.get("status") or "",
                    "action_type": "find_in_page",
                    "query_count": 0, "source_count": len(call_sources)})
            else:
                ev("web_search_other", {"action": action})

        elif itype == "message":
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("output_text", "text"):
                    btext = block.get("text") or ""
                    answer_parts.append(btext)
                    for ann in block.get("annotations") or []:
                        if isinstance(ann, dict) and ann.get("type") in (
                                "url_citation", "url"):
                            url = ann.get("url") or ""
                            if not url:
                                continue
                            cu = canonical_url(url)
                            rec = cite_map.get(cu)
                            if rec is None:
                                rec = {"raw_url": url, "canonical_url": cu,
                                       "domain": domain_of(url),
                                       "title": ann.get("title") or "",
                                       "citation_count": 0,
                                       "first_annotation_index":
                                           ann.get("start_index")}
                                cite_map[cu] = rec
                            rec["citation_count"] += 1
                            # Every occurrence separately (claim mapping):
                            si, ei = ann.get("start_index"), ann.get("end_index")
                            gs = (answer_offset + si) if si is not None else None
                            ge = (answer_offset + ei) if ei is not None else None
                            occurrences.append({
                                "occurrence_number": len(occurrences) + 1,
                                "raw_url": url, "canonical_url": cu,
                                "domain": domain_of(url),
                                "title": ann.get("title") or "",
                                "start_index": gs, "end_index": ge,
                                "cited_text": (btext[si:ei]
                                               if si is not None
                                               and ei is not None else ""),
                                "answer_sentence": _sentence_around(
                                    btext, si) if si is not None else "",
                            })
                    answer_offset += len(btext) + 1  # blocks joined by \n
                elif block.get("type") == "refusal":
                    rtext = "[refusal] " + (block.get("refusal") or "")
                    answer_parts.append(rtext)
                    answer_offset += len(rtext) + 1
            ev("message", {"chars": sum(len(p) for p in answer_parts)})

    answer_text = "\n".join(p for p in answer_parts if p).strip()
    if not answer_text and raw.get("output_text"):
        answer_text = str(raw["output_text"]).strip()
    reasoning_summary = "\n\n".join(reasoning_parts).strip() or None
    citations = list(cite_map.values())

    # ---- entity attribution (bridge rows + per-entity run booleans) ------- #
    source_entity, entity_runs = [], []
    if matcher is not None and matcher.entities:
        seen_bridge = set()

        def bridge(url_c, dom, title, url_raw, stage):
            """One bridge row per (url, entity, relation, STAGE) — the stage
            a source-entity link was observed at is a distinct fact
            (search_source / opened_page / inpage_search / citation)."""
            for eid, rel in matcher.match_source(dom, title, url_raw):
                key = (url_c, eid, rel, stage)
                if key not in seen_bridge:
                    seen_bridge.add(key)
                    source_entity.append({
                        "canonical_url": url_c, "domain": dom,
                        "entity_id": eid, "relation": rel,
                        "evidence_stage": stage,
                        "evidence": (title or url_raw)[:200]})

        for s in sources:
            bridge(s["canonical_url"], s["domain"], s["title"], s["raw_url"],
                   "search_source")
        for o in opened:
            bridge(o["canonical_url"], o["domain"], "", o["raw_url"],
                   "opened_page")
        for i in inpage:
            bridge(i["canonical_url"], i["domain"], "", i["raw_url"],
                   "inpage_search")
        for c in citations:
            bridge(c["canonical_url"], c["domain"], c["title"], c["raw_url"],
                   "citation")

        answer_hits = set(matcher.match_text(answer_text))
        for e in matcher.entities:
            eid = e["entity_id"]
            # Attribution is tracked PER STAGE — returned in search results,
            # opened, searched in-page, cited — never conflated.
            src_returned = any(eid in matcher.match_domain(s["domain"])
                               for s in sources)
            pg_opened = any(eid in matcher.match_domain(o["domain"])
                            for o in opened)
            pg_searched = any(eid in matcher.match_domain(i["domain"])
                              for i in inpage)
            cited = any(eid in matcher.match_domain(c["domain"])
                        for c in citations)
            targeted = any(eid in json.loads(q["entity_hits"] or "[]")
                           for q in queries)
            name_in_sources = any(
                r["entity_id"] == eid and r["relation"] == "mentions"
                for r in source_entity)
            mentioned_anywhere = (eid in answer_hits or src_returned
                                  or pg_opened or pg_searched or targeted
                                  or cited or name_in_sources)
            entity_runs.append({
                "entity_id": eid,
                "mentioned_anywhere": int(mentioned_anywhere),
                "named_in_answer": int(eid in answer_hits),
                "search_targeted": int(targeted),
                "source_returned": int(src_returned),
                "page_opened": int(pg_opened),
                "page_searched": int(pg_searched),
                "cited": int(cited),
            })

    rollups = {
        "num_search_actions": n_search,
        "num_queries": sum(1 for q in queries if not q["query_missing"]),
        "num_queries_missing_text": sum(q["query_missing"] for q in queries),
        "num_site_queries": sum(q["has_site_operator"] for q in queries),
        "num_open_page": len(opened),
        "num_find_in_page": len(inpage),
        "num_search_sources": len(sources),
        "num_citations": sum(c["citation_count"] for c in citations),
        "num_unique_cited_urls": len(citations),
    }

    return {
        "events": events, "fanout_queries": queries,
        "search_sources": sources, "opened_pages": opened,
        "inpage_searches": inpage, "citations": citations,
        "citation_occurrences": occurrences, "search_calls": search_calls,
        "source_entity": source_entity, "entity_runs": entity_runs,
        "answer_text": answer_text, "reasoning_summary": reasoning_summary,
        "rollups": rollups, "actual_model": raw.get("model") or "",
        "response_id": raw.get("id") or "",
        "response_status": raw.get("status") or "",
        "incomplete_reason": ((raw.get("incomplete_details") or {})
                              .get("reason") or ""),
        "response_created_at": str(raw.get("created_at") or ""),
        "service_tier": raw.get("service_tier") or "",
        "api_error_code": str((raw.get("error") or {}).get("code") or "")
                          if isinstance(raw.get("error"), dict) else "",
        "api_error_message": str((raw.get("error") or {}).get("message")
                                 or "")[:500]
                             if isinstance(raw.get("error"), dict) else "",
        "usage": extract_usage(raw),
    }
