"""Universe loading and normalisation.

Input CSV columns (flexible, case-insensitive); MSCI ACWI constituent exports
are recognised via HEADER_ALIASES.
Output: list of normalised company dicts ready for entity mapping.
"""
import csv
import re
from pathlib import Path

REQUIRED = ["name"]
OPTIONAL = ["ticker", "isin", "lei", "country", "sector", "market_cap_usd", "issuer_id",
            "gics_industry_group", "gics_sub_industry"]

# Known source schemas: normalised source header -> canonical field
HEADER_ALIASES = {
    # MSCI ACWI constituent export
    "issuername": "name", "issuerticker": "ticker", "issuerisin": "isin",
    "issuercntrydomicile": "country", "gicssector": "sector",
    "marketcapusd": "market_cap_usd", "issuerid": "issuer_id",
    "gicsindgrp": "gics_industry_group", "gicssubind": "gics_sub_industry",
}

# Legal suffixes stripped when generating search aliases
_LEGAL = re.compile(
    r"\b(s\.?a\.?|s\.?e\.?|a\.?g\.?|inc\.?|ltd\.?|limited|llc|llp|plc|corp\.?|corporation|"
    r"company|co\.?|group|holdings?|n\.?v\.?|b\.?v\.?|ab|asa|spa|oyj|pte|pty|pcl|"
    r"berhad|bhd|aktiengesellschaft|aktiebolag|aktieselskab|kaisha|kabushiki|jsc|pjsc|saog|qsc|public|"
    r"a\.?s\.?|anonim|sirketi|b\.?m\.?|spolka|akcyjna|sanayi|ticaret|"
    r"societa\s+per\s+azioni|società\s+per\s+azioni|de\s+c\.?v\.?)\b",
    re.IGNORECASE,
)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


def load_universe(path: str | Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header_map = {}
        for h in (reader.fieldnames or []):
            nh = _norm_header(h)
            header_map[HEADER_ALIASES.get(nh, nh)] = h
        if "name" not in header_map:
            raise ValueError(f"universe file needs a name column; got {reader.fieldnames}")
        fields = REQUIRED + OPTIONAL
        for i, row in enumerate(reader):
            rec = {k: (row.get(header_map[k]) or "").strip() for k in fields if k in header_map}
            if not rec["name"]:
                continue
            rec["row"] = i
            rec["aliases"] = _aliases(rec["name"])
            rows.append(rec)
    return rows


def _aliases(name: str) -> list[str]:
    """Alias candidates for entity resolution: full name, no-leading-The,
    legal-suffix-stripped, ampersand variants."""
    out = [name]
    no_the = re.sub(r"^the\s+", "", name, flags=re.IGNORECASE).strip()
    if no_the and no_the.lower() != name.lower():
        out.append(no_the)
    for base in (name, no_the):
        stripped = _LEGAL.sub("", base)
        stripped = re.sub(r"\s{2,}", " ", stripped).strip(" .,-")
        if stripped and stripped.lower() not in {o.lower() for o in out}:
            out.append(stripped)
    if "&" in name:
        out.append(name.replace("&", "and"))
    return list(dict.fromkeys(out))
