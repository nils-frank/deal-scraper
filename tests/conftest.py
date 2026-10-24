"""Shared fixtures. Every offer here is synthetic: never commit real Marktguru
response data to this repo."""
from __future__ import annotations

import os
from datetime import date

import pytest

# main.py and ha_client.py read these at import time.
os.environ.setdefault("HA_TOKEN", "test-token")
os.environ.setdefault("DEAL_SCRAPER_PLZ", "00000")
os.environ.setdefault("NOTIFY_SERVICE", "test_device")

from schema import Offer  # noqa: E402

TODAY = date(2026, 10, 22)


def make_offer(
    offer_id: str = "1",
    title: str = "Testmarke - Frischkaese",
    retailer: str = "Markt A",
    price: float | None = 1.11,
    valid_from: date = date(2026, 10, 19),
    valid_to: date = date(2026, 10, 24),
) -> Offer:
    return Offer(
        title=title,
        brand=None,
        price=price,
        unit_price=None,
        unit=None,
        retailer=retailer,
        plz="00000",
        valid_from=valid_from,
        valid_to=valid_to,
        image_url=None,
        source_url=None,
        offer_id=f"marktguru:{offer_id}",
    )


@pytest.fixture
def conn(tmp_path):
    import store

    with store.open_store(str(tmp_path / "test.db")) as connection:
        yield connection
