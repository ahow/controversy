"""Scorecard calibration: label theme outcomes, snapshot early-life features,
fit a transparent logistic scorecard in pure Python (no new dependencies — the
GitHub Actions runner stays at pyyaml + requests).

The audit contract:
- 'High profile' (the outcome) is defined in parameters.yaml themes.outcome —
  a coverage-velocity rule (rolling window, min articles, min sources, within
  N days of first sighting), never vibes.
- Features are snapshotted as of first_seen + snapshot_days so the fitted
  weights describe what is knowable EARLY, not at the peak.
- fit() output is a PROPOSAL (weights + normalisation + diagnostics). Humans
  merge it into parameters.yaml; this module never self-modifies config.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta

from .themes import escalation_features


def label_outcomes(conn: sqlite3.Connection, tp: dict) -> dict:
    """Label every theme against the outcome rule using exact per-article
    evidence (not daily approximations): for each rolling window of
    outcome.window_days starting within outcome.within_days of first_seen,
    escalated = window articles >= min_articles AND distinct window sources
    >= min_sources.
    """
    o = tp["outcome"]
    out = {}
    for r in conn.execute("SELECT id, first_seen FROM themes").fetchall():
        tid, first = r["id"], r["first_seen"]
        evs = [x[0] for x in conn.execute(
            "SELECT event_uri FROM theme_events WHERE theme_id=?",
            (tid,)).fetchall()]
        if not evs or not first:
            out[tid] = {"label": 0, "reason": "no_events"}
            continue
        marks = ",".join("?" * len(evs))
        rows = conn.execute(
            f"SELECT date, source FROM articles WHERE event_uri IN ({marks}) "
            "ORDER BY date", evs).fetchall()
        by_day: dict[str, list] = {}
        for row in rows:
            by_day.setdefault(row["date"], []).append(row["source"])
        if not by_day:
            out[tid] = {"label": 0, "reason": "no_articles"}
            continue
        days = sorted(by_day)
        horizon = date.fromisoformat(first) + timedelta(days=o["within_days"])
        best = {"label": 0, "peak_articles": 0, "peak_sources": 0,
                "escalated_date": None}
        for i, start in enumerate(days):
            if date.fromisoformat(start) > horizon:
                break
            w_end = date.fromisoformat(start) + timedelta(days=o["window_days"] - 1)
            arts, srcs = 0, set()
            for d in days[i:]:
                if date.fromisoformat(d) > w_end:
                    break
                arts += len(by_day[d])
                srcs.update(s for s in by_day[d] if s)
            if arts > best["peak_articles"]:
                best.update(peak_articles=arts, peak_sources=len(srcs))
            if arts >= o["min_articles"] and len(srcs) >= o["min_sources"]:
                best.update(label=1, escalated_date=start,
                            peak_articles=arts, peak_sources=len(srcs))
                break
        best["days_tracked"] = len(days)
        out[tid] = best
    return out


def feature_snapshots(conn: sqlite3.Connection, tp: dict) -> dict:
    """Feature vector per theme as of first_seen + snapshot_days-1 — what was
    knowable at the decision point, not at the peak."""
    k = tp.get("snapshot_days", 3) - 1
    snaps = {}
    for r in conn.execute("SELECT id, first_seen FROM themes").fetchall():
        tid, first = r["id"], r["first_seen"]
        if not first:
            continue
        day = (date.fromisoformat(first) + timedelta(days=k)).isoformat()
        snaps[tid] = escalation_features(conn, tid, day, tp)
    return snaps


FEATURE_ORDER = ("accel", "spread_sources", "spread_langs", "authority",
                 "actors", "attach", "persist")


def _vectorise(snaps: dict, labels: dict) -> tuple[list, list, list]:
    tids, X, y = [], [], []
    for tid, f in snaps.items():
        if tid not in labels or labels[tid].get("days_tracked", 0) < 1:
            continue
        tids.append(tid)
        X.append([f.get(k) or 0.0 for k in FEATURE_ORDER])
        y.append(labels[tid]["label"])
    return tids, X, y


def _standardise(X: list) -> tuple[list, list, list]:
    n, d = len(X), len(FEATURE_ORDER)
    means = [sum(row[j] for row in X) / n for j in range(d)]
    stds = []
    for j in range(d):
        var = sum((row[j] - means[j]) ** 2 for row in X) / n
        stds.append(math.sqrt(var) or 1.0)
    Z = [[(row[j] - means[j]) / stds[j] for j in range(d)] for row in X]
    return Z, means, stds


def fit_logistic(X: list, y: list, lr: float = 0.1, epochs: int = 2000,
                 l2: float = 1e-3) -> dict:
    """Batch gradient descent, standardised features, L2 penalty. Deterministic
    (zero init), no RNG — same data always yields the same weights."""
    Z, means, stds = _standardise(X)
    n, d = len(Z), len(FEATURE_ORDER)
    w, b = [0.0] * d, 0.0
    for _ in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        for zi, yi in zip(Z, y):
            p = 1.0 / (1.0 + math.exp(-(sum(wj * zj for wj, zj in zip(w, zi)) + b)))
            err = p - yi
            for j in range(d):
                gw[j] += err * zi[j]
            gb += err
        for j in range(d):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * gb / n
    return {"weights": dict(zip(FEATURE_ORDER, [round(v, 6) for v in w])),
            "intercept": round(b, 6), "means": dict(zip(FEATURE_ORDER, means)),
            "stds": dict(zip(FEATURE_ORDER, stds))}


def auc(y: list, scores: list) -> float | None:
    """Rank-based AUC; None when the sample has a single class."""
    pos = [s for yy, s in zip(y, scores) if yy == 1]
    neg = [s for yy, s in zip(y, scores) if yy == 0]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0
               for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def calibrate(conn: sqlite3.Connection, tp: dict) -> dict:
    """Full pipeline: labels + snapshots -> fitted weights + diagnostics.
    Returns a proposal dict (persisted by the caller); the caller decides
    whether the evidence base is strong enough to touch parameters.yaml."""
    labels = label_outcomes(conn, tp)
    snaps = feature_snapshots(conn, tp)
    tids, X, y = _vectorise(snaps, labels)
    base_rate = round(sum(y) / len(y), 4) if y else None
    result = {"n_themes": len(y), "n_escalated": sum(y),
              "base_rate": base_rate,
              "labels": labels,
              "as_of": date.today().isoformat()}
    if len(set(y)) < 2 or len(y) < 10:
        # Honest refusal: fitting on one-class or tiny samples produces
        # confident nonsense. The proposal is marked NOT FIT for merge.
        result.update(status="insufficient_data",
                      note="Need >=10 themes with both outcome classes; "
                           "priors in parameters.yaml remain in force.")
        return result
    model = fit_logistic(X, y)
    Z, _, _ = _standardise(X)
    fitted = [1.0 / (1.0 + math.exp(-(sum(
        model["weights"][k] * zj for k, zj in zip(FEATURE_ORDER, zi))
        + model["intercept"]))) for zi in Z]
    result.update(status="fitted", model=model, auc=auc(y, fitted))
    return result
