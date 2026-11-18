"""Universe loading and normalisation.

Input CSV columns (flexible, case-insensitive):
  name (required), ticker, isin, lei, country, sector
Output: list of normalised company dicts ready for entity mapping.
"""
import csv
import re
from pathlib import Path

REQUIRED = ["name"]
OPTIONAL = ["ticker", "isin", "lei", "country", "sector"]

# Legal suffixes stripped when generating search aliases
_LEGAL = re.compile(
    r"\b(s\.?a\.?|s\.?e\.?|a\.?g\.?|inc\.?|ltd\.?|llc|llp|plc|corp\.?|corporation|"
    r"company|co\.?|group|holdings?|n\.?v\.?|b\.?v\.?|ab|asa|spa|oyj|pte|pty)\b",
    re.IGNORECASE,
)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


def load_universe(path: str | Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header_map = {_norm_header(h): h for h in (reader.fieldnames or [])}
        if "name" not in header_map:
            raise ValueError(f"universe file needs a name column; got {reader.fieldnames}")
        for i, row in enumerate(reader):
            rec = {k: (row.get(header_map[k]) or "").strip() for k in REQUIRED + OPTIONAL if k in header_map}
            if not rec["name"]:
                continue
            rec["row"] = i
            rec["aliases"] = _aliases(rec["name"])
            rows.append(rec)
    return rows


def _aliases(name: str) -> list[str]:
    """Alias candidates for entity resolution: full name, suffix-stripped, ampersand variants."""
    out = [name]
    stripped = _LEGAL.sub("", name)
    stripped = re.sub(r"\s{2,}", " ", stripped).strip(" .,-")
    if stripped and stripped.lower() != name.lower():
        out.append(stripped)
    if "&" in name:
        out.append(name.replace("&", "and"))
    return list(dict.fromkeys(out))
