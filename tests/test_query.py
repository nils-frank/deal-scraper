from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
import requests

from conftest import Clock, make_offer, raw_offer
from deals import marktguru, query
from deals.schema import LOCAL_TZ

SATURDAY = date(2026, 10, 24)
SUNDAY = date(2026, 10, 25)


def at(day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour), LOCAL_TZ).astimezone(timezone.utc)


def butter(offer_id, brand, product, retailer, price, unit_price, *, description="je 250 g", category="Butter",
           volume=0.25, valid_from=date(2026, 10, 19), valid_to=SATURDAY, **fields):
    return raw_offer(offer_id, brand=brand, product=product, description=description, retailer=retailer,
                     price=price, unit_price=unit_price, volume=volume, category=category,
                     valid_from=valid_from, valid_to=valid_to, **fields)


# A synthetic Saturday "Butter" search in the shape of a real one: several
# chains, one product at two prices, an app price, upcoming offers, and
# Butter-named products in other categories.
BUTTER_RESULTS = [
    butter(1, "Kleehof", "Butter", "Markt E", 1.79, 7.16, valid_from=date(2026, 10, 22)),
    butter(2, "Bergwiese", "Feine Butter", "Markt C", 1.29, 5.16, description="HINWEIS: MIT APP 0,10 € BONUS, je 250 g"),
    butter(3, "Almhof", "Butter", "Markt A", 0.99, 3.96),
    butter(4, "Bergwiese", "Feine Butter", "Markt B", 1.29, 5.16, description="HINWEIS: MIT APP 0,10 € BONUS, je 250 g"),
    butter(5, "Kleehof", "Butter", "Markt D", 1.69, 6.76, valid_from=date(2026, 10, 22)),
    butter(6, "Weidegold", "Beste Butter", "Markt F", 1.49, 5.96, valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31)),
    butter(7, "Streichgut", "Halbfettmargarine", "Markt A", 1.29, 2.87, description="je 450 g"),
    butter(8, "Almhof", "Butterkaese", "Markt B", 0.89, 8.90, category="Käse", description="je 100 g"),
    butter(9, "Kekshaus", "Butter-Kekse", "Markt A", 1.19, 5.95, category="Kekse"),
    butter(10, "Weidegold", "Butter", "Markt F", 1.39, 5.56, valid_from=date(2026, 11, 9), valid_to=date(2026, 11, 14)),
]


@pytest.fixture
def saturday(conn, fake_marktguru):
    fake_marktguru.results["Butter"] = BUTTER_RESULTS
    clock = Clock(at(SATURDAY))
    return marktguru.Client(conn, "00000", session=fake_marktguru, clock=clock)


# --- when ---


def offer_window(valid_from, valid_to, offer_id="1"):
    return make_offer(offer_id=offer_id, valid_from=valid_from, valid_to=valid_to)


THURSDAY = date(2026, 10, 22)


@pytest.mark.parametrize(
    ("valid_from", "valid_to", "when", "expected"),
    [
        # Window edges on a weekday.
        (date(2026, 10, 19), THURSDAY, "now", "current"),  # ends today
        (THURSDAY, date(2026, 10, 24), "now", "current"),  # starts today
        (date(2026, 10, 19), date(2026, 10, 21), "now", None),  # ended yesterday
        (date(2026, 10, 23), date(2026, 10, 24), "now", "upcoming"),  # starts tomorrow
        (date(2026, 10, 28), date(2026, 10, 31), "now", "upcoming"),  # last day of the horizon
        (date(2026, 10, 29), date(2026, 10, 31), "now", None),  # beyond the horizon
        (date(2026, 10, 28), date(2026, 10, 31), "week", "upcoming"),
        (date(2026, 10, 29), date(2026, 10, 31), "week", None),
        (date(2026, 10, 29), date(2026, 10, 31), "upcoming", "upcoming"),
        (date(2026, 11, 30), date(2026, 12, 5), "upcoming", "upcoming"),
        (date(2026, 10, 19), date(2026, 10, 24), "upcoming", None),
    ],
)
def test_when_on_a_weekday(valid_from, valid_to, when, expected):
    current, upcoming = query.split_by_when([offer_window(valid_from, valid_to)], when, THURSDAY)
    assert ("current" if current else "upcoming" if upcoming else None) == expected


@pytest.mark.parametrize(
    ("valid_from", "valid_to", "when", "expected"),
    [
        (date(2026, 10, 19), SATURDAY, "now", None),  # ended yesterday, Saturday
        (date(2026, 10, 26), date(2026, 10, 31), "now", "current"),  # starts Monday, the next opening day
        (date(2026, 10, 27), date(2026, 10, 31), "now", "upcoming"),
        (date(2026, 11, 1), date(2026, 11, 7), "now", "upcoming"),  # Monday + 6
        (date(2026, 11, 2), date(2026, 11, 7), "now", None),
        (date(2026, 11, 1), date(2026, 11, 7), "week", None),  # the week counts from today, Sunday
        (date(2026, 10, 26), date(2026, 10, 31), "upcoming", None),  # Monday is "now", not upcoming
        (date(2026, 10, 27), date(2026, 10, 31), "upcoming", "upcoming"),
    ],
)
def test_when_on_a_sunday(valid_from, valid_to, when, expected):
    assert query.reference_day(SUNDAY) == date(2026, 10, 26)
    current, upcoming = query.split_by_when([offer_window(valid_from, valid_to)], when, SUNDAY)
    assert ("current" if current else "upcoming" if upcoming else None) == expected


def test_unknown_when_is_rejected():
    with pytest.raises(ValueError):
        query.split_by_when([], "later", THURSDAY)


# --- labels ---


def test_validity_labels():
    ends_today = offer_window(date(2026, 10, 19), SATURDAY)
    later = offer_window(date(2026, 10, 19), date(2026, 10, 31))
    starts_monday = offer_window(date(2026, 10, 26), date(2026, 10, 31))
    assert query.validity_label_de(ends_today, SATURDAY) == "bis heute (Sa 24.10.)"
    assert query.validity_label_de(later, SATURDAY) == "bis Sa 31.10."
    assert query.validity_label_de(starts_monday, SATURDAY) == "ab Mo 26.10. bis Sa 31.10."
    assert query.validity_label_en(ends_today, SATURDAY) == "until today (Sat 24 Oct)"
    assert query.validity_label_en(starts_monday, SATURDAY) == "from Mon 26 Oct until Sat 31 Oct"


# --- find_offers ---


def test_groups_by_product_cheapest_first(saturday):
    result = query.find_offers(saturday, "Butter")

    assert [g.name for g in result.current] == ["Almhof Butter", "Bergwiese Feine Butter", "Kleehof Butter"]
    assert [o.retailer for o in result.current[1].offers] == ["Markt B", "Markt C"]
    assert [o.price for o in result.current[2].offers] == [1.69, 1.79]
    assert [g.name for g in result.upcoming] == ["Weidegold Beste Butter"]
    assert result.excluded_categories == {"Käse": 1, "Kekse": 1}


def test_same_price_sorts_by_unit_price(conn, fake_marktguru):
    fake_marktguru.results["Butter"] = [
        butter(1, "Kleehof", "Butter", "Markt A", 1.29, 5.16),
        butter(2, "Almhof", "Butter", "Markt A", 1.29, 3.23),
    ]
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=Clock(at(SATURDAY)))
    assert [g.name for g in query.find_offers(client, "Butter").current] == ["Almhof Butter", "Kleehof Butter"]


def test_text_output_matches_the_design_example(saturday):
    result = query.find_offers(saturday, "butter")

    assert query.format_text(result, at(SATURDAY) + timedelta(minutes=2)) == (
        "Butter on offer today, Sat 24 Oct:\n"
        "- Almhof Butter 250 g: 0.99 € (3.96 €/kg) at Markt A, until today (Sat 24 Oct)\n"
        "- Bergwiese Feine Butter 250 g: 1.29 € (5.16 €/kg) at Markt B and Markt C, until today (Sat 24 Oct), app price\n"
        "- Kleehof Butter 250 g: 1.69 € (6.76 €/kg) at Markt D; 1.79 € (7.16 €/kg) at Markt E; all until today (Sat 24 Oct)\n"
        "\n"
        "Coming up in the next days:\n"
        "- Weidegold Beste Butter 250 g: 1.49 € (5.96 €/kg) at Markt F, from Mon 26 Oct until Sat 31 Oct\n"
        "\n"
        "Left out as other categories: Käse (1), Kekse (1).\n"
        "Prices from Marktguru, fetched 2 minutes ago.\n"
        "29 of 30 live searches left today."
    )


def test_speech_output(saturday):
    result = query.find_offers(saturday, "Butter")

    assert query.format_speech(result, at(SATURDAY)) == (
        "Butter im Angebot: Almhof Butter 0,99 € bei Markt A, Bergwiese Feine Butter 1,29 € bei Markt B und Markt C, "
        "Kleehof Butter 1,69 € bei Markt D, alle bis heute. "
        "Demnächst: ab Montag bis 31. Oktober Weidegold Beste Butter 1,49 € bei Markt F."
    )


def test_sunday_answers_for_monday(conn, fake_marktguru):
    fake_marktguru.results["Butter"] = BUTTER_RESULTS
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=Clock(at(SUNDAY)))

    result = query.find_offers(client, "Butter")

    assert result.reference_day == date(2026, 10, 26)
    assert [g.name for g in result.current] == ["Weidegold Beste Butter"]
    assert query.format_text(result, at(SUNDAY)).splitlines()[:2] == [
        "Butter on offer on Mon 26 Oct (shops are closed today):",
        "- Weidegold Beste Butter 250 g: 1.49 € (5.96 €/kg) at Markt F, from Mon 26 Oct until Sat 31 Oct",
    ]


def test_upcoming_lists_everything_later(saturday):
    result = query.find_offers(saturday, "Butter", "upcoming")
    assert result.current == []
    assert [g.name for g in result.upcoming] == ["Weidegold Butter", "Weidegold Beste Butter"]


def test_result_carries_fetch_metadata_and_validity(saturday, fake_marktguru):
    first = query.find_offers(saturday, "Butter").to_dict()
    assert first["fetched_at"] == at(SATURDAY).isoformat()
    assert first["from_cache"] is False
    assert first["budget_left"] == 29
    almhof = first["current"][0]["offers"][0]
    assert almhof["validity"] == "bis heute (Sa 24.10.)"
    assert (almhof["valid_from"], almhof["valid_to"], almhof["store"]) == ("2026-10-19", "2026-10-24", "Markt A")
    assert first["upcoming"][0]["offers"][0]["validity"] == "ab Mo 26.10. bis Sa 31.10."

    again = query.find_offers(saturday, "Butter").to_dict()
    assert again["from_cache"] is True
    assert again["source"] == "cache"
    assert again["budget_left"] == 29
    assert fake_marktguru.searches == ["Butter"]


def test_store_filter(saturday):
    result = query.find_offers(saturday, "Butter", stores=["markt c"])
    assert [(g.name, [o.retailer for o in g.offers]) for g in result.current] == [("Bergwiese Feine Butter", ["Markt C"])]


def test_failed_search_answers_from_the_stored_fetch_of_that_query(saturday, fake_marktguru):
    query.find_offers(saturday, "Butter")
    fetched = saturday.clock()
    saturday.clock.advance(hours=13)
    fake_marktguru.fail = requests.ConnectionError("down")

    result = query.find_offers(saturday, "Butter")

    assert result.source == "store"
    assert result.fetched_at == fetched
    assert [g.name for g in result.current][:1] == ["Almhof Butter"]
    text = query.format_text(result, saturday.clock())
    assert text.endswith(
        "Marktguru not asked (Marktguru error: down); stored offers fetched 13 hours ago.\n"
        # The failed search counts too.
        "28 of 30 live searches left today."
    )
    assert query.format_speech(result, saturday.clock()).endswith("Stand: vor 13 Stunden.")


def test_failed_search_never_answers_from_other_queries(saturday, fake_marktguru):
    query.find_offers(saturday, "Butter")
    fake_marktguru.fail = requests.ConnectionError("down")

    result = query.find_offers(saturday, "Almhof")

    assert result.source == "store"
    assert result.fetched_at is None
    assert result.current == [] and result.upcoming == []
    assert query.format_speech(result, saturday.clock()) == "Für Almhof habe ich gerade keine Angebote gefunden."


def test_nothing_found(conn, fake_marktguru):
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=Clock(at(SATURDAY)))
    result = query.find_offers(client, "Kaviar")
    assert result.current == [] and result.upcoming == []
    assert query.format_text(result, at(SATURDAY)).startswith("Kaviar on offer today, Sat 24 Oct:\n- nothing found\n")
    assert query.format_speech(result, at(SATURDAY)) == "Für Kaviar habe ich gerade keine Angebote gefunden."


def test_never_writes_dedup_or_verdicts(saturday, conn):
    query.find_offers(saturday, "Butter")
    assert conn.execute("SELECT COUNT(*) FROM notified_offers").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0


def test_speech_names_each_end_when_they_differ(conn, fake_marktguru):
    fake_marktguru.results["Butter"] = [
        butter(1, "Almhof", "Butter", "Markt A", 0.99, 3.96),
        butter(2, "Kleehof", "Butter", "Markt B", 1.29, 5.16, valid_to=SATURDAY + timedelta(days=7)),
    ]
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=Clock(at(SATURDAY)))
    assert query.format_speech(query.find_offers(client, "Butter"), at(SATURDAY)) == (
        "Butter im Angebot: Almhof Butter 0,99 € bei Markt A bis heute, Kleehof Butter 1,29 € bei Markt B bis 31. Oktober."
    )


def test_line_merges_stores_by_what_is_shown(conn, fake_marktguru):
    fake_marktguru.results["Butter"] = [
        # Same price, different start dates that both lie in the past: one entry.
        butter(1, "Almhof", "Butter", "Markt A", 1.29, 5.16, valid_from=date(2026, 10, 19)),
        butter(2, "Almhof", "Butter", "Markt B", 1.29, 5.16, valid_from=date(2026, 10, 22)),
        # Different pack sizes in one group: each entry names its own.
        butter(3, "Kleehof", "Butter", "Markt A", 1.69, 8.45, volume=0.2),
        butter(4, "Kleehof", "Butter", "Markt C", 1.79, 7.16),
    ]
    client = marktguru.Client(conn, "00000", session=fake_marktguru, clock=Clock(at(SATURDAY)))

    lines = query.format_text(query.find_offers(client, "Butter"), at(SATURDAY)).splitlines()

    assert lines[1:3] == [
        "- Almhof Butter 250 g: 1.29 € (5.16 €/kg) at Markt A and Markt B, until today (Sat 24 Oct)",
        "- Kleehof Butter: 1.69 € (8.45 €/kg, 200 g) at Markt A; 1.79 € (7.16 €/kg, 250 g) at Markt C; all until today (Sat 24 Oct)",
    ]
