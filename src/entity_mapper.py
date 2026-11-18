"""Entity mapping: resolve universe companies to Event Registry concept URIs.

Strategy (in order):
1. suggestConceptsFast on each alias; keep candidates of type 'org'.
2. Exact-label match (normalised, diacritics stripped — 'ČEZ Group' matches 'CEZ')
   beats prefix match — 'S&P Global' wins over 'S&P Global Platts'.
3. Auto-accept only an unambiguous exact/single-strong org match; everything
   else routes to the QA queue — human review, never guess.
4. Early stop on the first unambiguous exact match keeps the full-universe run
   near 1 API call/company (the suggest endpoint is slow, ~10s/call).

Ambiguous names (Delta, Vale, Apple-as-fruit class) are the norm, so the QA
queue is a first-class output, not an error path.
"""
import re
from dataclasses import dataclass, field


@dataclass
class MapResult:
    name: str
    row: int
    status: str                      # auto_mapped | needs_review | unmapped
    concept_uri: str | None = None
    concept_label: str | None = None
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", (s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _strong_matches(orgs: list[dict], aliases: list[str]) -> list[dict]:
    norms = {_norm(a) for a in aliases}
    return [c for c in orgs if _norm(c["label"]) in norms
            or any(_norm(c["label"]).startswith(_norm(a)) and len(_norm(a)) >= 4 for a in aliases)]


def _exact_matches(orgs: list[dict], aliases: list[str]) -> list[dict]:
    norms = {_norm(a) for a in aliases}
    return [c for c in orgs if _norm(c["label"]) in norms]


def map_company(client, company: dict) -> MapResult:
    seen = {}
    for alias in company["aliases"]:
        try:
            for c in client.suggest_concepts(alias):
                uri = c.get("uri") or ""
                label = (c.get("label") or {}).get("eng", "")
                ctype = c.get("type", "")
                if uri and uri not in seen:
                    seen[uri] = {"uri": uri, "label": label, "type": ctype, "via": alias}
        except Exception as e:  # network hiccup: record and stop this company
            return MapResult(company["name"], company["row"], "unmapped", reason=f"api error: {e}")
        # Early stop: an unambiguous exact org match on this alias ends the search.
        orgs = [c for c in seen.values() if c["type"] == "org"]
        exact = _exact_matches(orgs, company["aliases"])
        if len(exact) == 1:
            c = exact[0]
            return MapResult(company["name"], company["row"], "auto_mapped",
                             concept_uri=c["uri"], concept_label=c["label"], candidates=orgs)

    orgs = [c for c in seen.values() if c["type"] == "org"]
    exact = _exact_matches(orgs, company["aliases"])
    strong = _strong_matches(orgs, company["aliases"])

    if len(exact) == 1:
        c = exact[0]
        return MapResult(company["name"], company["row"], "auto_mapped",
                         concept_uri=c["uri"], concept_label=c["label"], candidates=orgs)
    if len(exact) > 1:
        return MapResult(company["name"], company["row"], "needs_review",
                         candidates=exact, reason="multiple exact org candidates")
    if len(strong) == 1:
        c = strong[0]
        return MapResult(company["name"], company["row"], "auto_mapped",
                         concept_uri=c["uri"], concept_label=c["label"], candidates=orgs)
    if strong:
        return MapResult(company["name"], company["row"], "needs_review",
                         candidates=strong, reason="multiple strong org candidates")
    if orgs:
        return MapResult(company["name"], company["row"], "needs_review",
                         candidates=orgs, reason=f"{len(orgs)} weak org candidates")
    return MapResult(company["name"], company["row"], "unmapped",
                     candidates=list(seen.values())[:5], reason="no org candidates found")
