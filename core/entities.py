"""Tracked entities: definition, matching, attribution.

An entity is a dict:
    {entity_id, name, primary_domain, alt_domains[], aliases[],
     entity_type, role, notes}

role is one of primary | comparison | competitor | neutral — set by the user,
never inferred. Nothing here auto-classifies a non-primary entity as a
competitor.

Matching:
- domain match: primary_domain or any alt_domain, incl. subdomains
- name match: entity name or any alias, case-insensitive, boundary-aware
"""

from __future__ import annotations

import re
import uuid

from .model import domain_matches, domain_of


def new_entity(name: str, primary_domain: str = "", alt_domains=None,
               aliases=None, entity_type: str = "business",
               role: str = "comparison", notes: str = "",
               entity_id: str = "") -> dict:
    return {
        "entity_id": entity_id or ("ent_" + uuid.uuid4().hex[:8]),
        "name": (name or "").strip(),
        "primary_domain": domain_of(primary_domain) if primary_domain else "",
        "alt_domains": [domain_of(d) for d in (alt_domains or []) if d],
        "aliases": [a.strip() for a in (aliases or []) if a and a.strip()],
        "entity_type": entity_type or "business",
        "role": role if role in ("primary", "comparison", "competitor",
                                 "neutral") else "comparison",
        "notes": notes or "",
    }


def _compile(names):
    pats = []
    for n in names:
        n = (n or "").strip()
        if not n:
            continue
        pats.append(re.compile(r'(?<![A-Za-z0-9])' + re.escape(n)
                               + r'(?![A-Za-z0-9])', re.IGNORECASE))
    return pats


class EntityMatcher:
    def __init__(self, entities: list):
        self.entities = entities or []
        self._by_id = {e["entity_id"]: e for e in self.entities}
        self._compiled = []
        for e in self.entities:
            names = [e.get("name", "")] + list(e.get("aliases", []))
            domains = ([e.get("primary_domain", "")]
                       + list(e.get("alt_domains", [])))
            self._compiled.append({
                "entity_id": e["entity_id"],
                "patterns": _compile(names),
                "domains": [d for d in domains if d],
            })

    def get(self, entity_id: str) -> dict:
        return self._by_id.get(entity_id, {})

    def match_text(self, text: str) -> list:
        """entity_ids whose name/alias/domain string appears in `text`."""
        hits = []
        if not text:
            return hits
        low = text.lower()
        for c in self._compiled:
            if any(p.search(text) for p in c["patterns"]) \
                    or any(d and d in low for d in c["domains"]):
                hits.append(c["entity_id"])
        return hits

    def match_domain(self, host: str) -> list:
        """entity_ids for which `host` is a first-party domain."""
        host = (host or "").lower()
        return [c["entity_id"] for c in self._compiled
                if any(domain_matches(host, d) for d in c["domains"])]

    def match_source(self, host: str, title: str, url: str) -> list:
        """Attribution for a source: first-party domain OR name in title/url.
        Returns list of (entity_id, relation) — relation is 'first_party'
        when the domain belongs to the entity, else 'mentions'."""
        out = []
        fp = set(self.match_domain(host))
        for eid in fp:
            out.append((eid, "first_party"))
        text = " ".join(x for x in (title, url) if x)
        for eid in self.match_text(text):
            if eid not in fp:
                out.append((eid, "mentions"))
        return out
