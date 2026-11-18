"""Entity mapping: resolve universe companies to Event Registry concept URIs.

Strategy (in order):
1. suggestConceptsFast on each alias; keep candidates of type 'org' (or 'wiki'
   matching the full name with a corporate qualifier).
2. Auto-accept when exactly one org candidate and its label is a strong match
   (normalised label == an alias, or ticker match in label).
3. Otherwise route to the QA queue — human review, never guess.

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
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


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

    orgs = [c for c in seen.values() if c["type"] == "org"]
    strong = [c for c in orgs if _norm(c["label"]) in {_norm(a) for a in company["aliases"]}
              or any(_norm(c["label"]).startswith(_norm(a)) and len(_norm(a)) >= 4 for a in company["aliases"])]

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
