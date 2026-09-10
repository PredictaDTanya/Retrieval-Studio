"""Deterministic classifiers: source-domain classes, query taxonomy,
citation-gap diagnosis.

Source-domain classification and entity attribution are SEPARATE dimensions:
a domain has exactly one class (directory, review platform, …) regardless of
which entities its pages support — entity support lives in the source_entity
bridge table (see entities.EntityMatcher.match_source).
"""

from __future__ import annotations

import re

from .model import domain_matches

# --------------------------------------------------------------------------- #
# Source-domain classification (one class per canonical domain)
# --------------------------------------------------------------------------- #

DOMAIN_CLASSES = [
    "first_party", "directory", "review_platform", "government",
    "social_platform", "publication", "awards_site", "platform_docs",
    "other_third_party",
]

_DIRECTORIES = {
    "clutch.co", "goodfirms.co", "sortlist.com", "designrush.com",
    "upcity.com", "semrush.com", "agencies.semrush.com", "yelp.com",
    "expertise.com", "themanifest.com", "topseos.com", "wordofmouth.com.au",
    "trustedsearch.org", "oneflare.com.au", "yellowpages.com.au",
}
_REVIEWS = {
    "trustpilot.com", "productreview.com.au", "g2.com", "capterra.com",
    "glassdoor.com", "birdeye.com",
}
_SOCIAL = {
    "linkedin.com", "au.linkedin.com", "facebook.com", "instagram.com",
    "x.com", "twitter.com", "reddit.com", "youtube.com", "tiktok.com",
    "medium.com", "quora.com",
}
_PLATFORM_DOCS = {
    "developers.google.com", "support.google.com", "developer.mozilla.org",
    "search.google.com", "openai.com", "help.openai.com",
}
_PUBLICATIONS = {
    "smartcompany.com.au", "searchenginejournal.com", "searchengineland.com",
    "forbes.com", "afr.com", "news.com.au", "smh.com.au", "theage.com.au",
    "moz.com", "ahrefs.com", "semrush.com/blog",
}
_AWARDS_HINTS = ("award", "awards", "apacsearchawards", "finalist")


def classify_domain(domain: str, entity_domains=None) -> str:
    """One class per domain. entity_domains: all tracked entities' first-party
    domains — anything matching those is first_party regardless of the rest."""
    d = (domain or "").lower()
    if not d:
        return "other_third_party"
    for ed in (entity_domains or []):
        if domain_matches(d, ed):
            return "first_party"
    def _in(sset):
        return any(domain_matches(d, s) or d == s for s in sset)
    if _in(_DIRECTORIES):
        return "directory"
    if _in(_REVIEWS):
        return "review_platform"
    if d.endswith(".gov") or ".gov." in d or d.endswith(".gov.au"):
        return "government"
    if _in(_SOCIAL):
        return "social_platform"
    if _in(_PLATFORM_DOCS):
        return "platform_docs"
    if any(h in d for h in _AWARDS_HINTS):
        return "awards_site"
    if _in(_PUBLICATIONS):
        return "publication"
    return "other_third_party"


# --------------------------------------------------------------------------- #
# Query taxonomy — stable 15-category top level
# --------------------------------------------------------------------------- #

TAXONOMY = [
    "Agency discovery", "Reviews & reputation", "Case studies & proof",
    "Awards & recognition", "Pricing & commercial", "Local expertise",
    "Technical expertise", "Ecommerce expertise", "GEO & AI-search expertise",
    "Official / first-party verification", "Third-party comparison",
    "Government / authoritative guidance", "Direct entity investigation",
    "Freshness / year-specific", "Other",
]

_TAX_RULES = [
    # (category, compiled regex) — first match wins, ordered by specificity.
    ("GEO & AI-search expertise",
     r"\b(geo|generative engine|ai search|ai seo|llm|chatgpt|ai overview|"
     r"answer engine|aeo)\b"),
    ("Ecommerce expertise",
     r"\b(ecommerce|e-commerce|shopify|woocommerce|magento|online store)\b"),
    ("Technical expertise",
     r"\b(technical seo|site audit|core web vitals|schema|crawl|page ?speed|"
     r"structured data|indexing)\b"),
    ("Reviews & reputation",
     r"\b(review|reviews|rating|ratings|testimonial|reputation|trustpilot|"
     r"complaints?)\b"),
    ("Case studies & proof",
     r"\b(case stud(y|ies)|results|portfolio|success stor|clients?\b|"
     r"before and after|proof)\b"),
    ("Awards & recognition",
     r"\b(award|awards|finalist|winner|recognition|accredited|certified)\b"),
    ("Pricing & commercial",
     r"\b(pricing|price|cost|costs|fees|rates|affordable|cheap|budget|"
     r"how much|retainer)\b"),
    ("Government / authoritative guidance",
     r"\b(gov|government|accc|consumer affairs|fair trading|regulation|"
     r"licen[cs]e)\b|\.gov"),
    ("Official / first-party verification",
     r"\b(official (site|website)|homepage|about us|contact)\b"),
    ("Third-party comparison",
     r"\b(vs\.?|versus|compare|comparison|alternatives?|top \d+|best \d+|"
     r"list of)\b"),
    ("Agency discovery",
     r"\b(best|top|leading|recommended?)\b.*\b(agenc|compan|consultant|firm|"
     r"expert|specialist)|\b(agenc|consultant)\w*\b.*\b(hire|find|choose)\b"),
    ("Local expertise",
     r"\b(near me|local|melbourne|sydney|brisbane|perth|adelaide|victoria|"
     r"nsw|queensland|suburb)\b"),
    ("Freshness / year-specific", r"\b(20\d{2}|latest|current|this year)\b"),
]
_TAX_COMPILED = [(cat, re.compile(rx, re.IGNORECASE)) for cat, rx in _TAX_RULES]


def classify_query(query: str, matcher=None, site_domain: str = "") -> str:
    """Assign one stable top-level taxonomy category to a fan-out query.
    `matcher` (EntityMatcher) makes entity-name queries 'Direct entity
    investigation'; a site: probe on an entity domain counts as
    first-party verification."""
    q = query or ""
    if not q.strip():
        return "Other"
    if matcher is not None and matcher.match_text(q):
        # A tracked entity named in the query = direct investigation,
        # unless it's a site: probe on the entity's own domain.
        if site_domain and matcher.match_domain(site_domain):
            return "Official / first-party verification"
        return "Direct entity investigation"
    if site_domain and matcher is not None and matcher.match_domain(site_domain):
        return "Official / first-party verification"
    for cat, rx in _TAX_COMPILED:
        if rx.search(q):
            return cat
    return "Other"


# --------------------------------------------------------------------------- #
# Citation-gap diagnosis
# --------------------------------------------------------------------------- #

GAP_TYPES = {
    "first_party_service_page": "First-party service-page gap",
    "expert_entity": "Founder / expert-entity gap",
    "case_proof": "Case-study / proof gap",
    "review": "Review gap",
    "third_party_list": "Third-party list inclusion gap",
    "award_recognition": "Award / industry-recognition gap",
    "local_authority": "Local authority gap",
    "topical_relevance": "Topical relevance gap",
    "brand_investigation_failed": "Direct brand investigation failure",
    "crawl_access": "Crawl or access issue",
    "weak_first_party": "Weak first-party evidence",
    "insufficient_validation": "Insufficient independent validation",
}


def diagnose_gap(prompt_evidence: dict) -> list:
    """Classify the likely gap(s) for one prompt where the primary entity
    under-performs. `prompt_evidence` (all lists scoped to this prompt):

        primary_cited_runs / runs
        primary_searched (bool)     — any entity-targeted query fired
        primary_search_queries      — [query strings targeting the entity]
        primary_source_urls         — first-party URLs returned/opened
        primary_cited (bool)
        winner_domains              — [(domain, class, cites)] for the runs
        query_taxonomies            — Counter of taxonomy -> count

    Returns [(gap_key, human_label, supporting_evidence_str)].
    """
    out = []
    runs = max(prompt_evidence.get("runs", 1), 1)
    cited = prompt_evidence.get("primary_cited_runs", 0)
    searched = prompt_evidence.get("primary_searched", False)
    squeries = prompt_evidence.get("primary_search_queries", [])
    # fp_urls = first-party URLs RETURNED BY SEARCH (evidence_stage =
    # search_source); fp_any = first-party URLs seen at ANY stage.
    fp_urls = prompt_evidence.get("primary_source_urls", [])
    fp_any = prompt_evidence.get("primary_any_stage_urls", fp_urls)
    winners = prompt_evidence.get("winner_domains", [])
    tax = prompt_evidence.get("query_taxonomies", {})

    def add(key, evidence):
        out.append((key, GAP_TYPES[key], evidence))

    winner_classes = {}
    for dom, cls, cites in winners:
        winner_classes.setdefault(cls, []).append(f"{dom} ({cites})")

    if searched and not fp_any and not cited:
        add("brand_investigation_failed",
            "Model probed the entity but nothing came back at any stage: "
            + "; ".join(squeries[:3]))
        add("crawl_access",
            "Entity-targeted queries returned no first-party source — check "
            "indexability/robots for the probed properties.")
    if fp_urls and not cited:
        add("weak_first_party",
            "First-party pages were returned in search results but not "
            "cited: " + "; ".join(fp_urls[:3]))
    if "directory" in winner_classes:
        add("third_party_list",
            "Winning citations come from directories: "
            + "; ".join(winner_classes["directory"][:4]))
    if "review_platform" in winner_classes or tax.get("Reviews & reputation"):
        add("review",
            "Review signals decided runs here ("
            + "; ".join(winner_classes.get("review_platform", [])[:3])
            + f"; {tax.get('Reviews & reputation', 0)} review-intent queries).")
    if "awards_site" in winner_classes or tax.get("Awards & recognition"):
        add("award_recognition",
            "Awards/recognition sources or queries present: "
            + "; ".join(winner_classes.get("awards_site", [])[:3]))
    if tax.get("Case studies & proof"):
        add("case_proof",
            f"{tax['Case studies & proof']} proof-seeking queries fired; "
            "no first-party case-study asset was cited.")
    if tax.get("Local expertise") and cited == 0:
        add("local_authority",
            f"{tax['Local expertise']} local-intent queries; primary absent "
            "from local results.")
    if not searched and cited == 0:
        add("topical_relevance",
            "The model never targeted the entity at all across "
            f"{runs} runs — it is not yet associated with this topic.")
        add("first_party_service_page",
            "No entity-targeted retrieval: a dedicated page answering this "
            "prompt's intent is the entry requirement.")
    if cited and cited < runs and not any(
            k for k, _, _ in out if k not in ("weak_first_party",)):
        add("insufficient_validation",
            f"Cited in only {cited}/{runs} runs — first-party asset exists "
            "but independent validation is thin.")
    if not out:
        add("insufficient_validation",
            f"Primary cited in {cited}/{runs} runs; no dominant third-party "
            "pattern — broaden independent evidence.")
    # de-dup by key, preserve order
    seen, dedup = set(), []
    for k, label, ev in out:
        if k not in seen:
            seen.add(k)
            dedup.append((k, label, ev))
    return dedup
