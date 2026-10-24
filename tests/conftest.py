"""Shared fixtures. Every offer here is synthetic: never commit real Marktguru
response data to this repo."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone

import pytest

os.environ.setdefault("HA_TOKEN", "test-token")
os.environ.setdefault("DEAL_SCRAPER_PLZ", "00000")
os.environ.setdefault("NOTIFY_SERVICE", "test_device")

from deals import marktguru, store  # noqa: E402
from deals.schema import LOCAL_TZ, Offer  # noqa: E402

TODAY = date(2026, 10, 22)  # a Thursday
# 09:00 local time on TODAY.
NOW = datetime.combine(TODAY, time(9), LOCAL_TZ).astimezone(timezone.utc)

HOMEPAGE = '<html><script type="application/json">{"config": {"apiKey": "key-1234567", "clientKey": "client-1234567"}}</script></html>'


def make_offer(
    offer_id: str = "1",
    title: str = "Testmarke - Frischkaese",
    retailer: str = "Markt A",
    price: float | None = 1.11,
    valid_from: date = date(2026, 10, 19),
    valid_to: date = date(2026, 10, 24),
    **fields,
) -> Offer:
    """An offer whose title is exactly `title` (it goes into the product name)."""
    return Offer(
        offer_id=f"marktguru:{offer_id}",
        retailer=retailer,
        valid_from=valid_from,
        valid_to=valid_to,
        product=title,
        price=price,
        **fields,
    )


def _instant(day: date, at: time) -> str:
    """Marktguru's format: the UTC instant of a local wall-clock time."""
    return datetime.combine(day, at, LOCAL_TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_offer(
    offer_id: int = 12345,
    *,
    brand: str | None = None,
    product: str | None = "Frischkaese",
    description: str | None = None,
    retailer: str = "Markt A",
    price: float | None = 1.11,
    unit_price: float | None = None,
    volume: float | None = None,
    category: str | None = "Frischkaese",
    category_id: int = 7,
    valid_from: date = date(2026, 10, 19),
    valid_to: date = date(2026, 10, 24),
    **overrides,
) -> dict:
    """Synthetic search result in the shape of Marktguru's offer search API."""
    raw = {
        "id": offer_id,
        "brand": {"name": brand} if brand else None,
        "product": {"name": product} if product else None,
        "description": description,
        "advertisers": [{"name": retailer}],
        "industries": [{"name": "Supermarkt"}],
        "categories": [{"id": category_id, "name": category}] if category else [],
        "price": price,
        "oldPrice": None,
        "referencePrice": unit_price,
        "unit": {"shortName": "kg"},
        "volume": volume,
        "quantity": 1.0,
        "requiresLoyalityMembership": False,
        "validityDates": [{"from": _instant(valid_from, time(0)), "to": _instant(valid_to, time(23, 59))}],
        "images": {"count": 1},
    }
    raw.update(overrides)
    return raw


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeMarktguru:
    """Stands in for the requests.Session the Marktguru client uses: serves the
    homepage key block and `results[query]` for searches."""

    def __init__(self):
        self.results: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict | None]] = []
        self.fail: Exception | None = None
        self.reject_keys = 0  # answer this many searches with HTTP 401

    @property
    def searches(self) -> list[str]:
        return [params["q"] for url, params in self.calls if url == marktguru.SEARCH_ENDPOINT]

    @property
    def homepage_loads(self) -> int:
        return sum(1 for url, _ in self.calls if url == marktguru.HOME_URL)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if self.fail:
            raise self.fail
        if url == marktguru.HOME_URL:
            return FakeResponse(200, HOMEPAGE)
        if self.reject_keys:
            self.reject_keys -= 1
            return FakeResponse(401, "")
        # Marktguru's search ignores case.
        results = next((r for q, r in self.results.items() if q.casefold() == params["q"].casefold()), [])
        return FakeResponse(200, json.dumps({"totalResults": len(results), "results": results}))


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(db_path):
    with store.open_store(db_path) as connection:
        yield connection


@pytest.fixture
def fake_marktguru():
    return FakeMarktguru()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(conn, fake_marktguru, clock):
    return marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock)
