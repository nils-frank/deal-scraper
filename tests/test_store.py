from __future__ import annotations

import sqlite3
from datetime import date

import store
from conftest import TODAY, make_offer

EXPIRED = dict(valid_from=date(2026, 10, 12), valid_to=date(2026, 10, 17))


def test_already_notified_after_record(conn):
    offer = make_offer()
    assert not store.already_notified(conn, offer, "Frischkaese")
    store.record_notified(conn, offer, "Frischkaese", "Frischkaese")
    assert store.already_notified(conn, offer, "Frischkaese")
    assert not store.already_notified(conn, offer, "Butter")


def test_valid_offers_cheapest_first_and_includes_future(conn):
    store.record_notified(conn, make_offer(offer_id="1", retailer="Markt B", price=1.29), "Frischkaese")
    store.record_notified(conn, make_offer(offer_id="2", retailer="Markt A", price=1.11), "Frischkaese")
    future = make_offer(offer_id="3", retailer="Markt C", price=0.99, valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31))
    store.record_notified(conn, future, "Frischkaese")
    store.record_notified(conn, make_offer(offer_id="4", retailer="Markt D", price=0.49, **EXPIRED), "Frischkaese")
    store.record_notified(conn, make_offer(offer_id="5"), "Butter")

    offers = store.valid_offers(conn, "Frischkaese", TODAY)
    assert [(o.retailer, o.price) for o in offers] == [("Markt C", 0.99), ("Markt A", 1.11), ("Markt B", 1.29)]
    assert offers[0].valid_from == date(2026, 10, 26)


def test_offer_ending_today_is_still_valid(conn):
    store.record_notified(conn, make_offer(valid_to=TODAY), "Frischkaese")
    assert len(store.valid_offers(conn, "Frischkaese", TODAY)) == 1


def test_record_refreshes_details_of_existing_row(conn):
    store.record_notified(conn, make_offer(price=1.29), "Frischkaese", "Frischkaese")
    store.record_notified(conn, make_offer(price=1.11), "Frischkaese", "Frischkaese")
    assert [o.price for o in store.valid_offers(conn, "Frischkaese", TODAY)] == [1.11]


def test_prune_returns_items_with_no_valid_offer_left(conn):
    store.record_notified(conn, make_offer(offer_id="1", **EXPIRED), "Frischkaese", "Frischkaese")
    store.record_notified(conn, make_offer(offer_id="2", **EXPIRED), "Butter", "Butter")
    store.record_notified(conn, make_offer(offer_id="3"), "Butter", "Butter")

    assert store.prune_expired(conn, TODAY) == ["Frischkaese"]
    assert store.valid_offers(conn, "Frischkaese", TODAY) == []
    assert conn.execute("SELECT COUNT(*) FROM notified_offers").fetchone()[0] == 1


def test_migrates_legacy_schema(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE notified_offers (offer_id TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, "
        "watchlist_summary TEXT NOT NULL, notified_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "PRIMARY KEY (offer_id, valid_from, valid_to, watchlist_summary))"
    )
    legacy.execute(
        "INSERT INTO notified_offers (offer_id, valid_from, valid_to, watchlist_summary) VALUES (?, ?, ?, ?)",
        ("marktguru:1", "2026-10-19", "2026-10-24", "Frischkaese"),
    )
    legacy.commit()
    legacy.close()

    with store.open_store(db_path) as conn:
        # A legacy row has no display details yet, so it can't be rendered...
        assert store.valid_offers(conn, "Frischkaese", TODAY) == []
        # ...until the offer is matched again, which fills them in.
        store.record_notified(conn, make_offer(offer_id="1"), "Frischkaese", "Frischkaese")
        assert [o.retailer for o in store.valid_offers(conn, "Frischkaese", TODAY)] == ["Markt A"]
        angebote = conn.execute("SELECT angebote_summary FROM notified_offers").fetchone()[0]
        assert angebote == "Frischkaese"
