from __future__ import annotations

from datetime import date

import pytest
import requests

from conftest import raw_offer
from deals import marktguru, store
from deals.marktguru import _offer_to_normalized


# --- normalization ---


def test_normalizes_fields():
    offer = _offer_to_normalized(
        raw_offer(brand="Testmarke", description="Doppelrahmstufe, 175 g", price=1.109, unit_price=6.34,
                  volume=0.175, oldPrice=1.49, valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31))
    )
    assert offer.title == "Testmarke - Frischkaese - Doppelrahmstufe, 175 g"
    assert offer.name == "Testmarke Frischkaese"
    assert offer.retailer == "Markt A"
    assert offer.price == 1.11
    assert offer.old_price == 1.49
    assert offer.unit_price == 6.34
    assert offer.unit == "kg"
    assert offer.volume == 0.175
    assert offer.quantity == 1.0
    assert offer.pack_size == "175 g"
    assert offer.category == "Frischkaese"
    assert offer.category_id == 7
    assert offer.app_only is False
    assert offer.offer_id == "marktguru:12345"
    assert offer.source_url == "https://www.marktguru.de/offers/12345"
    assert offer.raw["id"] == 12345


def test_validity_dates_are_local_days():
    # Marktguru sends local midnight as 22:00 or 23:00 UTC on the day before.
    offer = _offer_to_normalized(
        raw_offer(validityDates=[{"from": "2026-10-25T23:00:00Z", "to": "2026-10-31T22:59:00Z"}])
    )
    assert (offer.valid_from, offer.valid_to) == (date(2026, 10, 26), date(2026, 10, 31))
    summer = _offer_to_normalized(
        raw_offer(validityDates=[{"from": "2026-10-18T22:00:00Z", "to": "2026-10-24T21:59:00Z"}])
    )
    assert (summer.valid_from, summer.valid_to) == (date(2026, 10, 19), date(2026, 10, 24))


def test_multipack_size():
    offer = _offer_to_normalized(raw_offer(volume=0.02, quantity=5.0))
    assert offer.pack_size == "5 x 20 g"


@pytest.mark.parametrize(
    ("overrides", "app_only"),
    [
        ({"requiresLoyalityMembership": True}, True),
        ({"description": "HINWEIS: MIT APP 0,10 € BONUS, je 250 g"}, True),
        ({"description": "HINWEIS: MIT MARKT A APP 0.99 €"}, True),
        ({"description": "Mit der Kundenkarte Plus App"}, True),
        ({"description": "Appenzeller Art, je 100 g"}, False),
        ({"description": "mit Apfelstuecken"}, False),
    ],
)
def test_app_only(overrides, app_only):
    assert _offer_to_normalized(raw_offer(**overrides)).app_only is app_only


def test_image_url_from_cdn_pattern():
    offer = _offer_to_normalized(raw_offer())
    assert offer.image_url == "https://cdn.marktguru.de/api/v1/offers/12345/images/default/0/small.webp"


def test_no_image_url_when_offer_has_no_images():
    assert _offer_to_normalized(raw_offer(images={"count": 0})).image_url is None


def test_non_grocery_industry_is_dropped():
    assert _offer_to_normalized(raw_offer(industries=[{"name": "Möbelhaus"}])) is None


def test_undated_offer_is_dropped():
    assert _offer_to_normalized(raw_offer(validityDates=[])) is None


def test_normalize_query():
    assert marktguru.normalize_query("  Bio   BUTTER ") == "bio butter"


# --- client: cache ---


def test_live_search_is_stored_with_every_offer(client, conn, fake_marktguru, clock):
    fake_marktguru.results["Butter"] = [raw_offer(1, product="Butter"), raw_offer(2, product="Butter", retailer="Markt B")]

    result = client.search("Butter")

    assert result.source == "live"
    assert not result.from_cache
    assert result.fetched_at == clock.now
    assert [o.offer_id for o in result.offers] == ["marktguru:1", "marktguru:2"]
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 2
    assert conn.execute("SELECT query_norm, ok, total_results FROM fetches").fetchall() == [("butter", 1, 2)]
    assert conn.execute("SELECT raw_json IS NOT NULL FROM offers").fetchall() == [(1,), (1,)]
    assert result.budget_left == marktguru.DAILY_BUDGET - 1


def test_same_query_within_ttl_is_served_from_cache(client, fake_marktguru, clock):
    fake_marktguru.results["Butter"] = [raw_offer(1, product="Butter"), raw_offer(2, product="Butter")]
    first = client.search("Butter")
    clock.advance(hours=11, minutes=59)

    cached = client.search("  butter ")

    assert fake_marktguru.searches == ["Butter"]
    assert cached.source == "cache"
    assert cached.from_cache
    assert cached.fetched_at == first.fetched_at
    assert [o.offer_id for o in cached.offers] == ["marktguru:1", "marktguru:2"]
    assert cached.budget_left == marktguru.DAILY_BUDGET - 1


def test_cache_expires_after_ttl(client, fake_marktguru, clock):
    client.search("Butter")
    clock.advance(hours=12)

    assert client.search("Butter").source == "live"
    assert fake_marktguru.searches == ["Butter", "Butter"]


def test_refetch_updates_offer_but_keeps_first_seen(client, conn, fake_marktguru, clock):
    fake_marktguru.results["Butter"] = [raw_offer(1, product="Butter", price=1.29)]
    client.search("Butter")
    clock.advance(hours=13)
    fake_marktguru.results["Butter"] = [raw_offer(1, product="Butter", price=1.19)]

    client.search("Butter")

    first_seen, last_seen, price = conn.execute("SELECT first_seen, last_seen, price FROM offers").fetchone()
    assert price == 1.19
    assert first_seen < last_seen


def test_narrowing_retries_with_first_word(client, fake_marktguru):
    fake_marktguru.results["Testmarke"] = [raw_offer(1, brand="Testmarke")]

    result = client.search_narrowing("Testmarke Natur")

    assert fake_marktguru.searches == ["Testmarke Natur", "Testmarke"]
    assert result.query == "Testmarke"
    assert len(result.offers) == 1
    # Both searches are cached, so asking again costs nothing.
    client.search_narrowing("Testmarke Natur")
    assert len(fake_marktguru.searches) == 2


def test_no_narrowing_to_a_short_first_word(client, fake_marktguru):
    fake_marktguru.results["De"] = [raw_offer(1, brand="Dextro")]

    result = client.search_narrowing("De Testmarke")

    assert fake_marktguru.searches == ["De Testmarke"]
    assert result.query == "De Testmarke"
    assert result.offers == []


def test_no_narrowing_after_a_marktguru_error(client, conn, fake_marktguru):
    client.search("Butter")
    fake_marktguru.fail = requests.ConnectionError("down")

    result = client.search_narrowing("Testmarke Natur")

    assert fake_marktguru.searches == ["Butter", "Testmarke Natur"]
    assert conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 2
    assert result.query == "Testmarke Natur"
    assert result.error == "Marktguru unavailable (connection failed)"


def test_narrowing_with_budget_spent_answers_from_the_narrowed_store(conn, fake_marktguru, clock):
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock, daily_budget=1)
    fake_marktguru.results["Testmarke"] = [raw_offer(1, brand="Testmarke")]
    client.search("Testmarke")
    clock.advance(hours=13)

    result = client.search_narrowing("Testmarke Natur")

    assert fake_marktguru.searches == ["Testmarke"]
    assert result.query == "Testmarke"
    assert result.source == "store"
    assert [o.offer_id for o in result.offers] == ["marktguru:1"]


@pytest.mark.parametrize(
    ("daily_budget", "searches"),
    [(1, ["Testmarke Natur"]), (2, ["Testmarke Natur", "Testmarke"])],
)
def test_empty_narrowed_search_keeps_the_original_answer(conn, fake_marktguru, clock, daily_budget, searches):
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock, daily_budget=daily_budget)

    result = client.search_narrowing("Testmarke Natur")

    assert fake_marktguru.searches == searches
    assert result.query == "Testmarke Natur"
    assert result.source == "live"
    assert result.fetched_at == clock.now
    assert result.error is None
    assert result.budget_left == 0


# --- client: keys ---


def test_keys_are_cached_across_clients(conn, fake_marktguru, clock):
    marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock).search("Butter")
    clock.advance(hours=5)
    marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock).search("Milch")
    assert fake_marktguru.homepage_loads == 1

    clock.advance(hours=1)
    marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock).search("Eier")
    assert fake_marktguru.homepage_loads == 2


def test_stale_keys_are_refreshed_once_and_both_attempts_count(client, conn, fake_marktguru):
    fake_marktguru.results["Butter"] = [raw_offer(1, product="Butter")]
    fake_marktguru.reject_keys = 1

    result = client.search("Butter")

    assert result.source == "live"
    assert len(result.offers) == 1
    assert fake_marktguru.homepage_loads == 2
    assert conn.execute("SELECT ok FROM fetches ORDER BY id").fetchall() == [(-1,), (1,)]
    assert result.budget_left == marktguru.DAILY_BUDGET - 2


# --- client: budget and fallback ---


def test_budget_is_shared_and_hard(db_path, fake_marktguru, clock):
    # Two processes on one store file, each with its own connection.
    with store.open_store(db_path) as conn_a, store.open_store(db_path) as conn_b:
        a = marktguru.Client(conn_a, "00000", session=fake_marktguru, clock=clock, daily_budget=3)
        b = marktguru.Client(conn_b, "00000", session=fake_marktguru, clock=clock, daily_budget=3)
        a.search("eins")
        b.search("zwei")
        a.search("drei")

        exhausted = b.search("vier")

    assert fake_marktguru.searches == ["eins", "zwei", "drei"]
    assert exhausted.source == "store"
    assert exhausted.budget_left == 0
    assert exhausted.error == "daily search budget used up"
    assert exhausted.offers == [] and exhausted.fetched_at is None


def test_exhausted_budget_answers_from_store_with_fetch_age(conn, fake_marktguru, clock):
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock, daily_budget=2)
    fake_marktguru.results["Butter"] = [raw_offer(1, product="Butter")]
    client.search("Butter")
    fetched = clock.now
    clock.advance(hours=13)  # cache expired, same day
    client.search("Milch")

    result = client.search("Butter")

    assert fake_marktguru.searches == ["Butter", "Milch"]
    assert result.source == "store"
    assert result.from_cache
    assert result.fetched_at == fetched
    assert [o.offer_id for o in result.offers] == ["marktguru:1"]


def test_budget_resets_at_local_midnight(conn, fake_marktguru, clock):
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock, daily_budget=1)
    client.search("eins")
    assert client.budget_left() == 0
    clock.advance(hours=14, minutes=59)  # 23:59 local
    assert client.search("zwei").source == "store"

    clock.advance(minutes=1)  # 00:00 local

    assert client.search("zwei").source == "live"


def test_marktguru_error_answers_from_store(client, fake_marktguru, clock):
    fake_marktguru.results["Butter"] = [raw_offer(1, product="Butter")]
    client.search("Butter")
    fetched = clock.now
    clock.advance(days=3)
    fake_marktguru.fail = requests.ConnectionError("down")

    result = client.search("Butter")

    assert result.source == "store"
    assert result.error == "Marktguru unavailable (connection failed)"
    assert [o.offer_id for o in result.offers] == ["marktguru:1"]
    assert result.fetched_at == fetched


def test_unexpected_response_counts_and_falls_back(client, conn, fake_marktguru):
    fake_marktguru.results["Butter"] = "not a list"

    result = client.search("Butter")

    assert result.source == "store"
    assert result.offers == []
    assert conn.execute("SELECT ok FROM fetches").fetchall() == [(-1,)]
