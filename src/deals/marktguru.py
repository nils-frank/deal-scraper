"""Marktguru client with a key cache, a per-query cache and a shared daily budget.

Reference implementation: https://github.com/benmillerat/openclaw-supermarket-deals
Marktguru's internal search API needs no registration: an `apiKey` + `clientKey`
pair is embedded in a `<script type="application/json">` block on the homepage and
accepted as request headers.

Marktguru's terms prohibit automated extraction, so traffic stays at "one person
browsing" scale (DESIGN.md, "Marktguru traffic"). Every caller - the watchlist run,
`deals find`, the deal service - goes through `Client`, and all of them share
the store:
- the keys are cached for KEY_TTL;
- a search younger than QUERY_TTL for the same normalized query is answered from
  the store without a request;
- at most DAILY_BUDGET live searches per local day, counted in `fetches`;
- once the budget is spent or Marktguru fails, the answer is the newest stored
  result for that query, and the result says how old it is.
Nothing is ever fetched speculatively.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional

import requests

from deals import store
from deals.schema import LOCAL_TZ, Offer, local_date, utcnow

log = logging.getLogger(__name__)

HOME_URL = "https://www.marktguru.de"
# Same image URL the Marktguru web frontend uses for an offer's thumbnail; it is
# derived from the offer id, so it costs no extra request.
IMAGE_URL_TEMPLATE = "https://cdn.marktguru.de/api/v1/offers/{offer_id}/images/default/0/small.webp"
SEARCH_ENDPOINT = "https://api.marktguru.de/api/v1/offers/search"
USER_AGENT = "deal-scraper/1.0 (+homelab)"
REQUEST_TIMEOUT = 15
SEARCH_LIMIT = 40

KEY_TTL = timedelta(hours=6)
QUERY_TTL = timedelta(hours=12)
DAILY_BUDGET = 30
KEYS_KV = "marktguru_keys"

# Offer "industries" (Marktguru's own retailer-category tagging, from
# /api/v1/industries) that actually sell groceries. Without this, a
# single-brand watchlist entry like "Philadelphia" fuzzy-matches 100% against
# any offer carrying that brand name regardless of what's being sold - e.g. a
# "WMF Besteck-Set Philadelphia" cutlery set at XXXLutz (industry
# "Möbelhaus"). Retailers here (Lidl, EDEKA, Getränke Hoffmann, ...) can still
# run non-food weekly specials, but that's a rarer, in-scope-store edge case
# distinct from the wrong-store false positive actually observed.
GROCERY_INDUSTRIES = {"Supermarkt", "Discounter", "Getränkemarkt"}

# App and loyalty-card prices that requiresLoyalityMembership misses only show up
# in the flyer text: "HINWEIS: MIT APP 0,10 € BONUS", "MIT <CHAIN> APP 0.99 €".
APP_HINT = re.compile(r"\bmit\s+(?:der\s+)?(?:[\w-]+\s+){0,2}app\b", re.IGNORECASE)


class MarktguruError(RuntimeError):
    pass


class StaleKeys(MarktguruError):
    pass


BUDGET_USED_UP = "daily search budget used up"


class BudgetExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchResult:
    query: str                     # the query actually searched, after any narrowing
    offers: list[Offer]
    fetched_at: Optional[datetime]  # when Marktguru returned these offers; None if never
    source: str                    # "live", "cache" (younger than QUERY_TTL) or "store" (fallback)
    budget_left: int
    error: Optional[str] = None    # why no live search answered, for source "store"

    @property
    def from_cache(self) -> bool:
        return self.source != "live"


def _failure_reason(exc: Exception) -> str:
    """A short reason for user-facing answers. requests puts the full request URL,
    postal code included, into its messages, so those never get through."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection failed"
    if isinstance(exc, MarktguruError):
        return str(exc)
    return "unexpected response"


def normalize_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def _extract_keys(html: str) -> tuple[str, str]:
    for match in re.finditer(
        r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', html, re.S | re.I
    ):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        config = parsed.get("config") if isinstance(parsed, dict) else None
        if isinstance(config, dict) and config.get("apiKey") and config.get("clientKey"):
            return config["apiKey"], config["clientKey"]
    raise MarktguruError("could not extract apiKey/clientKey from Marktguru homepage")


def _parse_date(raw: Optional[str]) -> Optional[date]:
    """Marktguru sends validity bounds as UTC instants of local midnight and local
    23:59, so the local date is the one shoppers see on the flyer."""
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return local_date(moment) if moment.tzinfo else moment.date()


def _number(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _offer_to_normalized(raw: dict) -> Optional[Offer]:
    offer_id = raw.get("id")
    if offer_id is None:
        return None
    offer_id = str(offer_id)

    industries = {i.get("name") for i in raw.get("industries") or []}
    if industries and not (industries & GROCERY_INDUSTRIES):
        log.debug("Dropping offer_id=%s: non-grocery industry %r", offer_id, industries)
        return None

    advertisers = raw.get("advertisers") or []
    categories = raw.get("categories") or []
    category = categories[0] if categories else {}
    validity = raw.get("validityDates") or []
    description = raw.get("description")
    price = _number(raw.get("price"))
    old_price = _number(raw.get("oldPrice"))
    unit_price = _number(raw.get("referencePrice"))

    offer = Offer(
        offer_id=f"marktguru:{offer_id}",
        retailer=(advertisers[0].get("name") if advertisers else None) or "unknown",
        valid_from=_parse_date(validity[0].get("from")) if validity else None,
        valid_to=_parse_date(validity[0].get("to")) if validity else None,
        brand=(raw.get("brand") or {}).get("name"),
        product=(raw.get("product") or {}).get("name"),
        description=description,
        price=round(price, 2) if price is not None else None,
        old_price=round(old_price, 2) if old_price is not None else None,
        unit_price=round(unit_price, 2) if unit_price is not None else None,
        unit=(raw.get("unit") or {}).get("shortName"),
        volume=_number(raw.get("volume")),
        quantity=_number(raw.get("quantity")),
        category=category.get("name"),
        category_id=category.get("id"),
        app_only=bool(raw.get("requiresLoyalityMembership")) or bool(description and APP_HINT.search(description)),
        image_url=(
            IMAGE_URL_TEMPLATE.format(offer_id=offer_id)
            if offer_id.isdigit() and (raw.get("images") or {}).get("count", 1) != 0
            else None
        ),
        source_url=f"https://www.marktguru.de/offers/{offer_id}" if offer_id.isdigit() else None,
        raw=raw,
    )
    if not offer.title:
        return None
    if offer.valid_from is None or offer.valid_to is None:
        # Undated offers can't be matched against a validity window later - skip.
        log.debug("Dropping offer_id=%s title=%r: no valid validityDates (%r)", offer_id, offer.title, validity)
        return None
    return offer


class Client:
    def __init__(
        self,
        conn,
        plz: str,
        *,
        session: Optional[requests.Session] = None,
        daily_budget: int = DAILY_BUDGET,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.conn = conn
        self.plz = plz
        self.session = session or new_session()
        self.daily_budget = daily_budget
        self.clock = clock

    def budget_left(self) -> int:
        return max(0, self.daily_budget - store.searches_since(self.conn, self._day_start(self.clock())))

    def search(self, query: str) -> SearchResult:
        """One query: from the cache when it is younger than QUERY_TTL, else one
        live search within the daily budget, else the newest stored result."""
        query_norm = normalize_query(query)
        now = self.clock()
        cached = store.latest_fetch(self.conn, query_norm, self.plz, since=now - QUERY_TTL)
        if cached:
            log.info("search(%r): cached from %s", query, cached.fetched_at)
            return SearchResult(query, store.fetch_offers(self.conn, cached.id), cached.fetched_at, "cache", self.budget_left())
        try:
            offers = self._live_search(query, query_norm)
        except BudgetExhausted:
            return self._from_store(query, query_norm, BUDGET_USED_UP)
        except (MarktguruError, requests.RequestException, ValueError) as exc:
            log.warning("Marktguru search failed for %r: %s", query, exc)
            return self._from_store(query, query_norm, f"Marktguru unavailable ({_failure_reason(exc)})")
        return SearchResult(query, offers, now, "live", self.budget_left())

    def search_narrowing(self, query: str) -> SearchResult:
        """Marktguru's search only matches brand/product-name fields, not the flyer
        description - a watchlist entry with extra descriptive words ("Philadelphia
        Nature", "Alpro Not M*lk") that aren't in the offer's title can return 0
        results even though the item is on offer under a shorter name. Retries once
        with just the first word (typically the brand) and leaves precision to the
        matcher. Never retries after a Marktguru error, so an outage costs one
        search, and keeps the original answer when the retry finds nothing too.
        Skips the retry when the first word is 2 characters or fewer ("De Cecco"):
        such a search only returns unrelated offers the matcher rejects."""
        result = self.search(query)
        words = query.split()
        answered = result.source != "store" or result.error == BUDGET_USED_UP
        if result.offers or len(words) < 2 or len(words[0]) <= 2 or not answered:
            return result
        log.info("No offers for %r, retrying with narrowed query %r", query, words[0])
        narrowed = self.search(words[0])
        return narrowed if narrowed.offers else replace(result, budget_left=narrowed.budget_left)

    def _live_search(self, query: str, query_norm: str) -> list[Offer]:
        for attempt in range(2):
            now = self.clock()
            fetch_id = store.reserve_search(self.conn, query_norm, self.plz, now, self._day_start(now), self.daily_budget)
            if fetch_id is None:
                log.warning("Daily budget of %d live searches used up, not searching %r", self.daily_budget, query)
                raise BudgetExhausted()
            try:
                results, total = self._request(query, *self._keys(refresh=attempt > 0))
            except StaleKeys as exc:
                if attempt:
                    raise
                log.warning("Marktguru keys rejected, refreshing once: %s", exc)
                continue
            offers = []
            for raw in results:
                offer = _offer_to_normalized(raw)
                if offer is not None:
                    offers.append(offer)
            store.finish_fetch(self.conn, fetch_id, now, offers, total)
            log.info(
                "search(%r): %d raw result(s), %d normalized, %d dropped, %d search(es) left today",
                query, len(results), len(offers), len(results) - len(offers), self.budget_left(),
            )
            return offers
        raise AssertionError("unreachable")

    def _from_store(self, query: str, query_norm: str, reason: str) -> SearchResult:
        fetch = store.latest_fetch(self.conn, query_norm, self.plz)
        if fetch is None:
            return SearchResult(query, [], None, "store", self.budget_left(), reason)
        log.info("search(%r): %s, answering from the fetch of %s", query, reason, fetch.fetched_at)
        return SearchResult(query, store.fetch_offers(self.conn, fetch.id), fetch.fetched_at, "store", self.budget_left(), reason)

    def _keys(self, refresh: bool) -> tuple[str, str]:
        now = self.clock()
        cached = store.get_kv(self.conn, KEYS_KV)
        if cached and not refresh and now - cached[1] < KEY_TTL:
            keys = json.loads(cached[0])
            return keys["api_key"], keys["client_key"]
        resp = self.session.get(HOME_URL, headers={"Accept": "text/html"}, timeout=REQUEST_TIMEOUT)
        log.info("GET %s -> %d (%d bytes)", HOME_URL, resp.status_code, len(resp.content))
        resp.raise_for_status()
        api_key, client_key = _extract_keys(resp.text)
        log.info("Extracted Marktguru keys: apiKey=%s... clientKey=%s...", api_key[:6], client_key[:6])
        store.set_kv(self.conn, KEYS_KV, json.dumps({"api_key": api_key, "client_key": client_key}), now)
        return api_key, client_key

    def _request(self, query: str, api_key: str, client_key: str) -> tuple[list[dict], int]:
        params = {"as": "web", "limit": str(SEARCH_LIMIT), "q": query, "zipCode": self.plz}
        log.info("GET %s q=%r", SEARCH_ENDPOINT, query)
        resp = self.session.get(
            SEARCH_ENDPOINT,
            params=params,
            headers={"x-apikey": api_key, "x-clientkey": client_key, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        log.info("  -> %d (%d bytes)", resp.status_code, len(resp.content))
        if resp.status_code in (401, 403):
            raise StaleKeys(f"stale keys (HTTP {resp.status_code})")
        resp.raise_for_status()
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise MarktguruError("unexpected response shape: no 'results' array")
        total = body.get("totalResults")
        return results, total if isinstance(total, int) else len(results)

    @staticmethod
    def _day_start(now: datetime) -> datetime:
        """Local midnight of `now`'s day: the budget resets when shops' days do."""
        return datetime.combine(local_date(now), time(), LOCAL_TZ).astimezone(timezone.utc)
