"""Event Registry (newsapi.ai) client.

Design contract:
- Sequential requests only (API limit: 5 concurrent; we stay at 1).
- Token accounting on every call; raises BudgetExceeded if over the run budget.
- Pagination handled explicitly; each page is a separate token.
- Retries: bounded retries with 5s backoff; no retry loops.
- suggestConceptsFast is slow (~10s/call); ER_SUGGEST_TIMEOUT / ER_SUGGEST_RETRIES
  env vars tune it (use 25/1 for bulk retry passes, 60/2 for first passes).
"""
import time
import requests

BASE = "https://newsapi.ai/api/v1"


class BudgetExceeded(RuntimeError):
    pass


def is_quota_error(exc: requests.HTTPError) -> bool:
    """ER rejects calls with 401/402/403 once the monthly token allowance is
    exhausted (403 observed 2026-11-22). Signals 'stop gracefully, keep
    checkpoints' — NOT a code failure. Other HTTP errors fail loudly."""
    return (isinstance(exc, requests.HTTPError)
            and exc.response is not None
            and exc.response.status_code in (401, 402, 403))


class ERClient:
    def __init__(self, api_key: str, token_budget: int = 150, session: requests.Session | None = None):
        self.api_key = api_key
        self.budget = token_budget
        self.spent = 0
        self.s = session or requests.Session()

    def _check(self, cost: int):
        if self.spent + cost > self.budget:
            raise BudgetExceeded(f"call would cost {cost}; spent {self.spent}/{self.budget}")

    def _post(self, path: str, payload: dict, cost: int, retries: int = 1) -> dict:
        self._check(cost)
        payload = {**payload, "apiKey": self.api_key}
        for attempt in range(retries + 1):
            try:
                r = self.s.post(f"{BASE}{path}", json=payload, timeout=30)
                r.raise_for_status()
                self.spent += cost
                return r.json()
            except requests.RequestException:
                if attempt >= retries:
                    raise
                time.sleep(5)

    def _get(self, path: str, params: dict, retries: int = 1, timeout: int = 30) -> dict:
        for attempt in range(retries + 1):
            try:
                r = self.s.get(f"{BASE}{path}", params={**params, "apiKey": self.api_key}, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt >= retries:
                    raise
                time.sleep(5)

    # --- free (non-token) endpoints -------------------------------------
    def suggest_concepts(self, prefix: str, lang: str = "eng") -> list[dict]:
        """Concept suggestion for entity mapping. Free; filter type=='org' downstream."""
        import os
        js = self._get("/suggestConceptsFast", {"prefix": prefix, "lang": lang},
                       retries=int(os.environ.get("ER_SUGGEST_RETRIES", "2")),
                       timeout=int(os.environ.get("ER_SUGGEST_TIMEOUT", "60")))
        if isinstance(js, dict) and "error" in js:
            raise RuntimeError(js["error"])
        return js

    # --- token endpoints -------------------------------------------------
    def search_articles(self, query: dict, count: int = 100, page: int = 1,
                        sort_by: str = "date", date_start: str | None = None,
                        date_end: str | None = None, cost: int = 1) -> dict:
        """Recent article search (1 token). Archive searches cost 5/searched year —
        caller must pass the right cost."""
        q = dict(query)
        if date_start or date_end:
            q.setdefault("dateStart", date_start)
            q.setdefault("dateEnd", date_end)
        return self._post("/article/getArticles", {
            "query": {"$query": q},
            "resultType": "articles",
            "articlesSortBy": sort_by,
            "articlesCount": min(count, 100),
            "articlesPage": page,
            "includeArticleConcepts": True,
            "includeArticleCategories": True,
            "includeArticleSentiment": True,
            "includeArticleEventUri": True,
        }, cost=cost)

    def search_events(self, query: dict, count: int = 50, page: int = 1, cost: int = 5) -> dict:
        """Event (story cluster) search: 5 tokens recent, 20/searched year archive."""
        return self._post("/event/getEvents", {
            "query": {"$query": dict(query)},
            "resultType": "events",
            "eventsSortBy": "date",
            "eventsCount": min(count, 50),
            "eventsPage": page,
        }, cost=cost)


def company_concept_query(concept_uris: list[str]) -> dict:
    """OR-batch of company concept URIs for sweep queries."""
    return {"$or": [{"conceptUri": u} for u in concept_uris]}
