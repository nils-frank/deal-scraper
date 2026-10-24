from __future__ import annotations

from datetime import date

from fetch_marktguru import _offer_to_normalized


def raw_offer(**overrides) -> dict:
    """Synthetic search result in the shape of Marktguru's offer search API."""
    raw = {
        "id": 12345,
        "brand": {"name": "Testmarke"},
        "product": {"name": "Frischkaese"},
        "description": "Doppelrahmstufe, 175 g",
        "advertisers": [{"name": "Markt A"}],
        "industries": [{"name": "Supermarkt"}],
        "price": 1.109,
        "referencePrice": 6.34,
        "unit": {"shortName": "kg"},
        "validityDates": [{"from": "2026-10-26T00:00:00Z", "to": "2026-10-31T23:59:59Z"}],
        "images": {"count": 1},
    }
    raw.update(overrides)
    return raw


def test_normalizes_fields():
    offer = _offer_to_normalized(raw_offer(), "00000")
    assert offer.title == "Testmarke - Frischkaese - Doppelrahmstufe, 175 g"
    assert offer.retailer == "Markt A"
    assert offer.price == 1.11
    assert offer.unit_price == 6.34
    assert offer.unit == "kg"
    assert offer.valid_from == date(2026, 10, 26)
    assert offer.valid_to == date(2026, 10, 31)
    assert offer.offer_id == "marktguru:12345"
    assert offer.source_url == "https://www.marktguru.de/offers/12345"


def test_image_url_from_cdn_pattern():
    offer = _offer_to_normalized(raw_offer(), "00000")
    assert offer.image_url == "https://cdn.marktguru.de/api/v1/offers/12345/images/default/0/small.webp"


def test_no_image_url_when_offer_has_no_images():
    assert _offer_to_normalized(raw_offer(images={"count": 0}), "00000").image_url is None


def test_non_grocery_industry_is_dropped():
    assert _offer_to_normalized(raw_offer(industries=[{"name": "Möbelhaus"}]), "00000") is None


def test_undated_offer_is_dropped():
    assert _offer_to_normalized(raw_offer(validityDates=[]), "00000") is None
