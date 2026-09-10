"""TEST / SAMPLE FIXTURES — fabricated Responses-API-shaped payloads.

Everything in this file is invented data for testing and for the in-app
"Sample study". None of it is live research output. Payload shapes mirror the
real API: web_search_call actions (search with queries[]/query, open_page,
find_in_page, action.sources), url_citation annotations, reasoning items
with summary parts, and usage blocks.
"""

from __future__ import annotations

from core.entities import new_entity
from core.prompts import new_prompt


def SAMPLE_ENTITIES():
    return [
        new_entity("Acme SEO", primary_domain="acmeseo.example",
                   aliases=["Acme"], role="primary",
                   entity_id="ent_acme"),
        new_entity("Rival Digital", primary_domain="rivaldigital.example",
                   role="competitor", entity_id="ent_rival"),
        new_entity("Neutral Consulting", primary_domain="neutral.example",
                   role="neutral", entity_id="ent_neutral"),
    ]


def SAMPLE_PROMPTS():
    return [
        new_prompt("Who are the best SEO agencies in Springfield?",
                   prompt_id="S01", category="discovery"),
        new_prompt("Recommend an AI search optimisation specialist.",
                   prompt_id="S02", category="geo"),
    ]


_WS_N = [0]


def _ws_id():
    _WS_N[0] += 1
    return f"ws_fixture_{_WS_N[0]:03d}"


def _search(queries=None, query=None, sources=None):
    a = {"type": "search"}
    if queries is not None:
        a["queries"] = queries
    if query is not None:
        a["query"] = query
    if sources is not None:
        a["sources"] = sources
    return {"type": "web_search_call", "id": _ws_id(),
            "status": "completed", "action": a}


def _open(url):
    return {"type": "web_search_call", "id": _ws_id(),
            "status": "completed",
            "action": {"type": "open_page", "url": url}}


def _find(url, pattern):
    return {"type": "web_search_call", "id": _ws_id(),
            "status": "completed",
            "action": {"type": "find_in_page", "url": url,
                       "pattern": pattern}}


def _reasoning(text):
    return {"type": "reasoning",
            "summary": [{"type": "summary_text", "text": text}]}


def _msg(text, citations):
    return {"type": "message", "role": "assistant", "content": [{
        "type": "output_text", "text": text,
        "annotations": [{"type": "url_citation", "url": u, "title": t,
                         "start_index": 10 * i, "end_index": 10 * i + 5}
                        for i, (u, t) in enumerate(citations)]}]}


def _usage(i, o, reasoning=0, cached=0):
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o,
            "output_tokens_details": {"reasoning_tokens": reasoning},
            "input_tokens_details": {"cached_tokens": cached}}


def rich_payload(model="gpt-5.6-sol", resp_id="resp_FIXTURE_RICH"):
    """Covers: reasoning summary, queries[] incl. site:, singular query
    fallback, missing-query search, sources, open_page, find_in_page,
    citations incl. tracking params, multi-entity mentions."""
    return {
        "id": resp_id, "model": model, "status": "completed",
        "usage": _usage(1500, 800, 300, 100),
        "output": [
            _reasoning("TEST FIXTURE reasoning summary: compare local "
                       "agencies via directories and official sites."),
            _search(
                queries=["best SEO agency Springfield",
                         "site:acmeseo.example services",
                         "Rival Digital reviews"],
                sources=[
                    {"url": "https://www.acmeseo.example/seo?utm_source=openai",
                     "title": "Acme SEO — Services"},
                    {"url": "https://rivaldigital.example/",
                     "title": "Rival Digital"},
                    {"url": "https://lists.example/top-agencies",
                     "title": "Top agencies list"},
                    {"type": "oai-weather"},  # non-URL real-time feed source
                ]),
            _search(query="Springfield digital marketing pricing"),
            _search(),  # search action with NO query text returned
            _open("https://www.acmeseo.example/about"),
            _find("https://rivaldigital.example/services", "pricing"),
            _msg("Acme SEO stands out locally; Rival Digital is also "
                 "frequently recommended.",
                 [("https://www.acmeseo.example/seo?utm_source=openai",
                   "Acme SEO — Services"),
                  ("https://rivaldigital.example/", "Rival Digital"),
                  ("https://www.acmeseo.example/seo",
                   "Acme SEO — Services")]),  # dupe canonical, count=2
        ],
    }


def plain_payload(model="gpt-5.6-sol", resp_id="resp_FIXTURE_PLAIN"):
    """No reasoning item, no entity hits, single search."""
    return {
        "id": resp_id, "model": model, "status": "completed",
        "usage": _usage(400, 200),
        "output": [
            _search(query="what does an seo agency do",
                    sources=[{"url": "https://guides.example/seo",
                              "title": "SEO guide"}]),
            _msg("An SEO agency improves organic visibility.",
                 [("https://guides.example/seo", "SEO guide")]),
        ],
    }


def incomplete_payload(model="gpt-5.6-sol"):
    """Response the API cut short — must be classified, never counted ok."""
    p = plain_payload(model=model, resp_id="resp_FIXTURE_INCOMPLETE")
    p["status"] = "incomplete"
    p["incomplete_details"] = {"reason": "max_output_tokens"}
    return p


def failed_payload(model="gpt-5.6-sol"):
    """Response whose body reports status=failed with an API error — the
    actual error must be retained, not just a generic status."""
    p = plain_payload(model=model, resp_id="resp_FIXTURE_FAILED")
    p["status"] = "failed"
    p["error"] = {"code": "server_error",
                  "message": "The model failed to complete (fixture)."}
    p["output"] = []
    return p


def mismatch_payload():
    """actual model differs from any gpt-5.6-sol request."""
    p = plain_payload(model="gpt-5.6-terra", resp_id="resp_FIXTURE_MISMATCH")
    return p


def sample_payloads():
    """(meta, raw) pairs for the in-app sample dataset. 2 prompts x 2 runs,
    plus one failed run kept as evidence that errors are preserved."""
    out = []
    combos = [("S01", 1, rich_payload(resp_id="resp_S01_1")),
              ("S01", 2, plain_payload(resp_id="resp_S01_2")),
              ("S02", 1, rich_payload(resp_id="resp_S02_1")),
              ("S02", 2, plain_payload(resp_id="resp_S02_2"))]
    prompts = {p["prompt_id"]: p["prompt_text"] for p in SAMPLE_PROMPTS()}
    for pid, rn, raw in combos:
        out.append(({"prompt_id": pid, "run_number": rn,
                     "timestamp": f"2026-09-01T0{rn}:00:00Z",
                     "location": "Springfield, Victoria, AU",
                     "prompt_text": prompts[pid],
                     "status": "ok", "error": "", "latency_ms": 1234}, raw))
    out.append(({"prompt_id": "S02", "run_number": 3,
                 "timestamp": "2026-09-01T03:00:00Z",
                 "location": "Springfield, Victoria, AU",
                 "prompt_text": prompts["S02"],
                 "status": "error",
                 "error": "RateLimitError: simulated (fixture)",
                 "latency_ms": 50}, None))
    return out
