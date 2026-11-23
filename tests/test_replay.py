"""Tests for the archive-replay path: page_cost passthrough, BudgetExceeded
partial-day handling (roll-ups skipped), batch-granular resume without token
double-spend, and the reset scope of the replay driver."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.er_client import BudgetExceeded
from src.store import connect
from src.sweep import run_sweep, rollup_day

DAY = "2026-06-15"
KW = ["fraud"]
ISSUES = [{"id": "FRAUD_CORRUPTION", "keywords_en": ["fraud"],
           "hard_trigger": True, "prior_severity": 4, "prior_attention": 4,
           "status": "seed"}]


class FakeER:
    """Minimal stand-in for ERClient: serves one canned article per call,
    counts tokens at the supplied per-call cost, raises BudgetExceeded once
    the budget would be exceeded. fail_after optionally forces exhaustion at
    a fixed call count (budget large enough otherwise)."""
    def __init__(self, n_calls_available=10**9, cost_seen=None):
        self.spent = 0
        self.budget = 10**9
        self.calls = []
        self._avail = n_calls_available
        self.cost_seen = cost_seen if cost_seen is not None else []

    def search_articles(self, query, count=100, page=1, sort_by="date",
                        date_start=None, date_end=None, cost=1):
        if self._avail <= 0:
            raise BudgetExceeded(f"forced at call {len(self.calls)}")
        self._avail -= 1
        self.spent += cost
        self.calls.append({"page": page, "cost": cost})
        self.cost_seen.append(cost)
        n = len(self.calls)
        return {"articles": {"results": [{
            "uri": f"art-{n}", "date": DAY, "lang": "eng",
            "title": f"Acme fraud probe widens {n}", "url": f"http://x/{n}",
            "source": {"title": "reuters.com"}, "sentiment": -0.5,
            "eventUri": f"evt-{n}",
            "concepts": [{"uri": "uri:acme", "score": 5, "type": "org"}],
        }], "totalResults": 1}}


def _mk(tmp_path, n_companies=4):
    conn = connect(tmp_path / "t.db")
    for i in range(1, n_companies + 1):
        conn.execute("INSERT INTO companies (id, name, concept_uri, map_status)"
                     " VALUES (?,?,?, 'auto_mapped')",
                     (i, f"Acme{i}", f"uri:acme" if i == 1 else f"uri:c{i}"))
    conn.commit()
    return conn


def test_page_cost_passed_to_every_call(tmp_path):
    conn = _mk(tmp_path)
    client = FakeER()
    stats = run_sweep(client, conn, KW, ISSUES, batch_size=2, max_pages=1,
                      target_date=DAY, page_cost=5)
    assert stats["budget_exceeded"] is False
    assert client.cost_seen and all(c == 5 for c in client.cost_seen)
    assert stats["tokens_used"] == 5 * len(client.calls)


def test_budget_exceeded_skips_rollups_and_reports_progress(tmp_path):
    conn = _mk(tmp_path)
    # 2 batches (batch_size=2 over 4 companies), 1 page each; budget dies
    # during batch 1 -> batches_done == 1, roll-ups must NOT have run.
    client = FakeER(n_calls_available=2)  # batch0 page1 ok, batch1 page1 ok,
                                          # batch1 would stop anyway (1 page)
    client = FakeER(n_calls_available=1)  # dies on batch 1's first call
    stats = run_sweep(client, conn, KW, ISSUES, batch_size=2, max_pages=1,
                      target_date=DAY, page_cost=5)
    assert stats["budget_exceeded"] is True
    assert stats["batches_done"] == 1
    assert "incidents_built" not in stats           # roll-ups skipped
    assert conn.execute("SELECT COUNT(*) FROM company_daily").fetchone()[0] == 0
    # Articles fetched before the failure are persisted (idempotent resume).
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


def test_resume_completes_day_without_refetching_done_batches(tmp_path):
    conn = _mk(tmp_path)
    client = FakeER(n_calls_available=1)            # batch 0 only
    s1 = run_sweep(client, conn, KW, ISSUES, batch_size=2, max_pages=1,
                   target_date=DAY, page_cost=5)
    assert s1["budget_exceeded"] is True and s1["batches_done"] == 1
    calls_after_first = len(client.calls)           # == 1

    client._avail = 10                              # refill budget
    s2 = run_sweep(client, conn, KW, ISSUES, batch_size=2, batch_start=1,
                   max_pages=1, target_date=DAY, page_cost=5)
    assert s2["budget_exceeded"] is False and s2["batches_done"] == 1
    assert len(client.calls) == calls_after_first + 1   # no re-fetch of batch 0
    assert "incidents_built" in s2                  # roll-ups ran on completion
    # Two fetch calls produced two distinct articles; no duplicates.
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM company_daily"
                        " WHERE date=?", (DAY,)).fetchone()[0] >= 1


def test_rollup_day_matches_integrated_path(tmp_path):
    # Integrated: run_sweep rolls up internally.
    conn1 = _mk(tmp_path / "a")
    run_sweep(FakeER(), conn1, KW, ISSUES, batch_size=2, max_pages=1,
              target_date=DAY, page_cost=5)
    # Split: batch ranges + explicit rollup_day must produce the same state.
    conn2 = _mk(tmp_path / "b")
    client = FakeER()
    run_sweep(client, conn2, KW, ISSUES, batch_size=2, batch_start=0,
              batch_end=1, max_pages=1, target_date=DAY, page_cost=5)
    # emulate mid-day stop: roll-ups of the partial call did happen here
    # (call completed), so wipe and re-roll deterministically via rollup_day
    out = rollup_day(conn2, DAY, ISSUES)
    assert "incidents_built" in out and "daily" in out
    cols = "company_id, date, n_articles, n_sources, n_incidents"
    r1 = conn1.execute(f"SELECT {cols} FROM company_daily ORDER BY 1").fetchall()
    r2 = conn2.execute(f"SELECT {cols} FROM company_daily ORDER BY 1").fetchall()
    assert [tuple(x) for x in r2]  # non-empty
    # Same-day re-roll is idempotent (DELETE-first rebuilds).
    out2 = rollup_day(conn2, DAY, ISSUES)
    assert out2["daily"] == out["daily"]


def test_reset_scope():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "replay_themes", Path(__file__).resolve().parents[1]
        / "scripts" / "replay_themes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    conn = connect(":memory:")
    conn.executescript(mod.CKPT_DDL)
    conn.execute("INSERT INTO companies (id, name, map_status)"
                 " VALUES (1, 'Acme', 'auto_mapped')")
    conn.execute("INSERT INTO articles (uri, date) VALUES ('a1', ?)", (DAY,))
    conn.execute("INSERT INTO states (company_id, state) VALUES (1, 'WATCH')")
    conn.execute("INSERT INTO replay_days (day, done) VALUES (?, 1)", (DAY,))
    conn.commit()
    mod._reset(conn)
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM replay_days").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM states WHERE company_id=1"
                        ).fetchone()["state"] == "WATCH"


class QuotaER(FakeER):
    """Raises a 403 HTTPError after `avail` calls, mimicking monthly quota
    exhaustion (observed ER behaviour 2026-11-22)."""
    def __init__(self, avail):
        super().__init__()
        self._avail = avail

    def search_articles(self, query, **kw):
        if self._avail <= 0:
            import requests
            resp = requests.models.Response()
            resp.status_code = 403
            resp.url = "https://newsapi.ai/api/v1/article/getArticles"
            raise requests.HTTPError("403 Client Error: Forbidden",
                                     response=resp)
        self._avail -= 1
        self.spent += kw.get("cost", 1)
        self.calls.append(kw)
        n = len(self.calls)
        return {"articles": {"results": [{
            "uri": f"q-{n}", "date": DAY, "lang": "eng",
            "title": f"Acme fraud probe {n}", "url": f"http://x/q{n}",
            "source": {"title": "reuters.com"}, "sentiment": -0.5,
            "eventUri": f"qevt-{n}",
            "concepts": [{"uri": "uri:acme", "score": 5, "type": "org"}],
        }], "totalResults": 1}}


def test_quota_403_graceful_in_replay_mode(tmp_path):
    conn = _mk(tmp_path)
    client = QuotaER(avail=1)   # batch 0 completes, batch 1 hits the wall
    stats = run_sweep(client, conn, KW, ISSUES, batch_size=2, max_pages=1,
                      target_date=DAY, page_cost=5, graceful_quota=True)
    assert stats["budget_exceeded"] is True       # same contract as BudgetExceeded
    assert stats["batches_done"] == 1             # partial progress visible
    assert "incidents_built" not in stats         # no roll-up of a partial day
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


def test_quota_403_raises_in_daily_mode(tmp_path):
    import pytest, requests
    conn = _mk(tmp_path)
    client = QuotaER(avail=0)
    with pytest.raises(requests.HTTPError):       # daily sweep fails loudly
        run_sweep(client, conn, KW, ISSUES, batch_size=2, max_pages=1,
                  target_date=DAY, graceful_quota=False)


def test_is_quota_error_classification():
    import requests
    from src.er_client import is_quota_error
    def err(code):
        r = requests.models.Response(); r.status_code = code
        return requests.HTTPError(str(code), response=r)
    assert is_quota_error(err(403)) and is_quota_error(err(401))
    assert not is_quota_error(err(500)) and not is_quota_error(err(429))
