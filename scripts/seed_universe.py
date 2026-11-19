#!/usr/bin/env python3
"""Seed/repair the companies table from universe/entity_map_part*.csv.

Idempotent (INSERT OR IGNORE on row id): safe to run before every sweep.
Exists so CI (GitHub Actions) can bootstrap an empty database on cache miss
without any manual upload. Empty concept_uri is stored as NULL (the UNIQUE
constraint would otherwise collide on repeated empty strings).
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import ROOT
from src.store import connect


def main():
    parts = sorted((ROOT / "universe").glob("entity_map_part*.csv"))
    if not parts:
        sys.exit("no universe files found: universe/entity_map_part*.csv")
    conn = connect(ROOT / "data" / "controversy.db")
    n = 0
    for part in parts:
        with open(part, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                conn.execute(
                    "INSERT OR IGNORE INTO companies"
                    " (id,name,ticker,isin,country,sector,concept_uri,map_status)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (int(r["row"]), r["issuer_name"], r["ticker"], r["isin"],
                     r["country"], r["gics_sector"],
                     r["concept_uri"] or None, r["map_status"]))
                n += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    mapped = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE map_status='auto_mapped'").fetchone()[0]
    print(f"seeded {n} rows across {len(parts)} parts "
          f"({total} companies, {mapped} auto_mapped)")


if __name__ == "__main__":
    main()
