from __future__ import annotations

import sqlite3
from datetime import date

from datetime import datetime, timedelta

from conftest import NOW, TODAY, make_offer
from deals import store

EXPIRED = dict(valid_from=date(2026, 10, 12), valid_to=date(2026, 10, 17))


def test_already_notified_after_record(conn):
    offer = make_offer()
    assert not store.already_notified(conn, offer, "Frischkaese")
    store.record_notified(conn, offer, "Frischkaese", "Frischkaese")
    assert store.already_notified(conn, offer, "Frischkaese")
    assert not store.already_notified(conn, offer, "Butter")


def test_valid_offers_includes_future_and_skips_expired(conn):
    store.record_notified(conn, make_offer(offer_id="1", retailer="Markt B", price=1.29), "Frischkaese")
    store.record_notified(conn, make_offer(offer_id="2", retailer="Markt A", price=1.11), "Frischkaese")
    future = make_offer(offer_id="3", retailer="Markt C", price=0.99, valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31))
    store.record_notified(conn, future, "Frischkaese")
    store.record_notified(conn, make_offer(offer_id="4", retailer="Markt D", price=0.49, **EXPIRED), "Frischkaese")
    store.record_notified(conn, make_offer(offer_id="5"), "Butter")

    offers = store.valid_offers(conn, "Frischkaese", TODAY)
    assert sorted((o.retailer, o.price, o.valid_from) for o in offers) == [
        ("Markt A", 1.11, date(2026, 10, 19)),
        ("Markt B", 1.29, date(2026, 10, 19)),
        ("Markt C", 0.99, date(2026, 10, 26)),
    ]


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


V1_NOTIFIED = (
    "CREATE TABLE notified_offers (offer_id TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, "
    "watchlist_summary TEXT NOT NULL, notified_at TEXT NOT NULL DEFAULT (datetime('now')), "
    "angebote_summary TEXT, title TEXT, retailer TEXT, price REAL, "
    "PRIMARY KEY (offer_id, valid_from, valid_to, watchlist_summary))"
)


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
        ("marktguru:1", "2026-10-18", "2026-10-24", "Frischkaese"),
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


def test_migrates_v1_database_in_place(tmp_path):
    """The database the batch run wrote before v2: only notified_offers, with
    valid_from as the UTC date of Marktguru's start instant (a day early)."""
    db_path = str(tmp_path / "v1.db")
    v1 = sqlite3.connect(db_path)
    v1.execute("PRAGMA journal_mode=WAL")
    v1.execute(V1_NOTIFIED)
    v1.execute(
        "INSERT INTO notified_offers (offer_id, valid_from, valid_to, watchlist_summary, angebote_summary, title, retailer, price) "
        "VALUES ('marktguru:1', '2026-10-18', '2026-10-24', 'Frischkaese', 'Frischkaese', 'Frischkaese Natur', 'Markt A', 1.11)"
    )
    v1.commit()
    v1.close()

    with store.open_store(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"offers", "fetches", "fetch_offers", "matches", "notified_offers", "kv"} <= tables
        # The v1 row now carries the local start date, so the same offer is still
        # recognized as already pushed.
        assert store.already_notified(conn, make_offer(offer_id="1", valid_from=date(2026, 10, 19)), "Frischkaese")
        assert [(o.title, o.price, o.valid_from) for o in store.valid_offers(conn, "Frischkaese", TODAY)] == [
            ("Frischkaese Natur", 1.11, date(2026, 10, 19))
        ]

    # Opening it again changes nothing.
    with store.open_store(db_path) as conn:
        assert conn.execute("SELECT valid_from FROM notified_offers").fetchall() == [("2026-10-19",)]


def test_new_store_uses_wal_and_busy_timeout(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == store.BUSY_TIMEOUT_MS
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_second_connection_sees_writes(db_path, conn):
    store.record_notified(conn, make_offer(), "Frischkaese", "Frischkaese")
    with store.open_store(db_path) as other:
        assert store.already_notified(other, make_offer(), "Frischkaese")


def _fetch(conn, now, offers, query="butter"):
    fetch_id = store.reserve_search(conn, query, "00000", now, now - timedelta(hours=1), 30)
    store.finish_fetch(conn, fetch_id, now, offers, len(offers))
    return fetch_id


def test_prune_enforces_retention(conn):
    old = make_offer(offer_id="1", raw={"id": 1}, valid_from=date(2025, 10, 13), valid_to=date(2025, 10, 18))
    recent = make_offer(offer_id="2", raw={"id": 2}, valid_from=date(2026, 9, 1), valid_to=date(2026, 9, 5))
    current = make_offer(offer_id="3", raw={"id": 3})
    _fetch(conn, NOW - timedelta(days=91), [old], "alt")
    _fetch(conn, NOW - timedelta(days=31), [recent], "neu")
    _fetch(conn, NOW, [current], "jetzt")
    store.record_match(conn, "Butter", "marktguru:1", 90, "match", "fuzzy", NOW - timedelta(days=366))
    store.record_match(conn, "Butter", "marktguru:3", 90, "match", "fuzzy", NOW)

    store.prune(conn, NOW)

    # Offers: 12 months after they ended. raw_json: 30 days after last fetch.
    assert conn.execute("SELECT offer_key, raw_json IS NOT NULL FROM offers ORDER BY offer_key").fetchall() == [
        ("marktguru:2", 0),
        ("marktguru:3", 1),
    ]
    # Fetch log: 90 days, with its offer links.
    assert [row[0] for row in conn.execute("SELECT query_norm FROM fetches ORDER BY id")] == ["neu", "jetzt"]
    assert conn.execute("SELECT COUNT(*) FROM fetch_offers").fetchone()[0] == 2
    assert [row[0] for row in conn.execute("SELECT offer_key FROM matches")] == ["marktguru:3"]


def test_prune_keeps_offer_ended_just_under_a_year_ago(conn):
    edge = make_offer(offer_id="1", valid_from=date(2025, 10, 20), valid_to=TODAY - timedelta(days=365))
    _fetch(conn, NOW, [edge])
    store.prune(conn, NOW)
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1


def test_offers_round_trip(conn):
    offer = make_offer(brand="Testmarke", description="je 250 g", category="Butter", category_id=166, old_price=1.49,
                       unit_price=4.44, unit="kg", volume=0.25, quantity=1.0, app_only=True,
                       image_url="https://example.invalid/1.webp", source_url="https://example.invalid/1")
    fetch_id = _fetch(conn, NOW, [offer])
    assert store.fetch_offers(conn, fetch_id) == [offer]


def test_kv_round_trip(conn):
    store.set_kv(conn, "k", "v1", NOW)
    store.set_kv(conn, "k", "v2", NOW + timedelta(hours=1))
    assert store.get_kv(conn, "k") == ("v2", datetime.fromisoformat(store.to_iso(NOW + timedelta(hours=1))))
