from __future__ import annotations

from datetime import date

import main
from conftest import TODAY, make_offer

NEXT_WEEK = dict(valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31))


def test_current_offer_shows_end_date_only():
    assert main.format_todo_description(make_offer(), TODAY) == "1.11 € bei Markt A, gueltig bis 2026-10-24"


def test_offer_starting_today_is_current():
    offer = make_offer(valid_from=TODAY)
    assert main.format_validity(offer, TODAY) == "gueltig bis 2026-10-24"


def test_future_offer_shows_start_date():
    offer = make_offer(**NEXT_WEEK)
    assert main.format_todo_description(offer, TODAY) == "1.11 € bei Markt A, gueltig ab 2026-10-26 bis 2026-10-31"


def test_unknown_price():
    assert main.format_todo_description(make_offer(price=None), TODAY).startswith("? bei Markt A")


def test_angebote_line_keeps_title_and_start_date():
    line = main.format_angebote_line(make_offer(**NEXT_WEEK), TODAY)
    assert line == "Testmarke - Frischkaese - 1.11 € bei Markt A, gueltig ab 2026-10-26 bis 2026-10-31"


def test_watchlist_description_lists_every_store_cheapest_first():
    offers = [
        make_offer(retailer="Markt C", price=1.29),
        make_offer(retailer="Markt A", price=1.11),
        make_offer(retailer="Markt B", price=None),
        make_offer(retailer="Markt D", price=1.11),
    ]
    assert main.format_watchlist_description(offers, TODAY) == (
        "- 1.11 € bei Markt A, gueltig bis 2026-10-24\n"
        "- 1.11 € bei Markt D, gueltig bis 2026-10-24\n"
        "- 1.29 € bei Markt C, gueltig bis 2026-10-24\n"
        "- ? bei Markt B, gueltig bis 2026-10-24"
    )


def test_watchlist_description_keeps_cheapest_current_and_upcoming_offer_per_store():
    offers = [
        make_offer(offer_id="1", retailer="Markt A", price=1.49),
        make_offer(offer_id="2", retailer="Markt A", price=0.99, **NEXT_WEEK),
        make_offer(offer_id="3", retailer="Markt A", price=1.29),
        make_offer(offer_id="4", retailer="Markt A", price=1.09, **NEXT_WEEK),
        make_offer(offer_id="5", retailer="Markt B", price=0.89, **NEXT_WEEK),
        make_offer(offer_id="6", retailer="Markt B", price=1.39),
    ]
    assert main.format_watchlist_description(offers, TODAY) == (
        "- 1.29 € bei Markt A, gueltig bis 2026-10-24\n"
        "- 1.39 € bei Markt B, gueltig bis 2026-10-24\n"
        "- 0.89 € bei Markt B, gueltig ab 2026-10-26 bis 2026-10-31\n"
        "- 0.99 € bei Markt A, gueltig ab 2026-10-26 bis 2026-10-31"
    )


def test_watchlist_description_is_none_without_offers():
    assert main.format_watchlist_description([], TODAY) is None


def test_push_message_for_current_offers():
    assert main.format_push_message([make_offer(), make_offer()], TODAY) == "2 neue(s) Angebot(e) gefunden - siehe Einkaufsliste."


def test_push_message_names_future_start_dates():
    offers = [
        make_offer(),
        make_offer(valid_from=date(2026, 11, 2), valid_to=date(2026, 11, 7)),
        make_offer(**NEXT_WEEK),
        make_offer(**NEXT_WEEK),
    ]
    assert main.format_push_message(offers, TODAY) == (
        "4 neue(s) Angebot(e) gefunden, davon 2 ab 2026-10-26, 1 ab 2026-11-02 - siehe Einkaufsliste."
    )
