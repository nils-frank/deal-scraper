"""SQLite offer store (schema v2).

One file on the host volume mounted at /data (see deploy/deal-scraper.service),
shared by every process that reads or fetches offers. WAL mode plus a busy
timeout let the batch run and the deal service (`deals serve`) use it
at the same time; every multi-statement write takes the write lock up front
with BEGIN IMMEDIATE.

Tables:
- offers: every fetched offer, upserted on each fetch (12 months, raw_json 30 days).
- fetches + fetch_offers: one row per live Marktguru search and the offers it
  returned, in order. This is the query cache and the daily search budget (90 days).
- matches: every watchlist verdict with the method that decided it (12 months).
- notified_offers: the push dedup table, keyed on offer ID + validity window.
  Each row also keeps the display details (title, retailer, price) of the offer
  it dedups, so the watchlist descriptions can be rebuilt from the still-valid
  rows on every run. Only the watchlist run writes it.
- kv: small runtime state, such as the cached Marktguru keys and the time and
  items of the last finished watchlist run.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from deals.schema import Offer, local_date, to_iso

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/data/deal-scraper.db"
SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 30_000
LAST_WATCHLIST_RUN_KV = "last_watchlist_run"

# Retention (decision 3).
OFFER_RETENTION = timedelta(days=365)
RAW_JSON_RETENTION = timedelta(days=30)
FETCH_LOG_RETENTION = timedelta(days=90)
MATCH_RETENTION = timedelta(days=365)

NOTIFIED_SCHEMA = """
CREATE TABLE IF NOT EXISTS notified_offers (
    offer_id    TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    watchlist_summary TEXT NOT NULL,
    notified_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (offer_id, valid_from, valid_to, watchlist_summary)
)
"""

# Columns added to notified_offers after the first deployment, as (name, SQL
# type). Rows written before a column existed keep NULL in it until the offer
# is matched again.
ADDED_COLUMNS = [
    ("angebote_summary", "TEXT"),
    ("title", "TEXT"),
    ("retailer", "TEXT"),
    ("price", "REAL"),
]

V2_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS offers (
        offer_key   TEXT PRIMARY KEY,
        retailer    TEXT NOT NULL,
        brand       TEXT,
        product     TEXT,
        description TEXT,
        category    TEXT,
        category_id INTEGER,
        price       REAL,
        old_price   REAL,
        unit_price  REAL,
        unit        TEXT,
        volume      REAL,
        quantity    REAL,
        app_only    INTEGER NOT NULL DEFAULT 0,
        valid_from  TEXT NOT NULL,
        valid_to    TEXT NOT NULL,
        image_url   TEXT,
        source_url  TEXT,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL,
        raw_json    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS offers_category_valid ON offers (category, valid_to)",
    "CREATE INDEX IF NOT EXISTS offers_validity ON offers (valid_from, valid_to)",
    """
    CREATE TABLE IF NOT EXISTS fetches (
        id            INTEGER PRIMARY KEY,
        query_norm    TEXT NOT NULL,
        plz           TEXT NOT NULL,
        fetched_at    TEXT NOT NULL,
        ok            INTEGER NOT NULL DEFAULT 0,
        total_results INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS fetches_query ON fetches (query_norm, plz, fetched_at)",
    "CREATE INDEX IF NOT EXISTS fetches_time ON fetches (fetched_at)",
    """
    CREATE TABLE IF NOT EXISTS fetch_offers (
        fetch_id  INTEGER NOT NULL REFERENCES fetches (id) ON DELETE CASCADE,
        offer_key TEXT NOT NULL REFERENCES offers (offer_key) ON DELETE CASCADE,
        position  INTEGER NOT NULL,
        PRIMARY KEY (fetch_id, offer_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS fetch_offers_offer ON fetch_offers (offer_key)",
    """
    CREATE TABLE IF NOT EXISTS matches (
        id             INTEGER PRIMARY KEY,
        watchlist_item TEXT NOT NULL,
        offer_key      TEXT NOT NULL,
        score          REAL NOT NULL,
        decision       TEXT NOT NULL,
        method         TEXT NOT NULL CHECK (method IN ('fuzzy', 'category', 'llm')),
        decided_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS matches_time ON matches (decided_at)",
    """
    CREATE TABLE IF NOT EXISTS kv (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
]

OFFER_COLUMNS = (
    "offer_key", "retailer", "brand", "product", "description", "category", "category_id",
    "price", "old_price", "unit_price", "unit", "volume", "quantity", "app_only",
    "valid_from", "valid_to", "image_url", "source_url",
)


@dataclass(frozen=True)
class StoredOffer:
    title: str
    retailer: str
    price: float | None
    valid_from: date
    valid_to: date


@dataclass(frozen=True)
class Fetch:
    id: int
    fetched_at: datetime


@contextmanager
def open_store(db_path: str, *, migrate: bool = True):
    """migrate=False skips the schema check, for a long-running process that
    migrated once at startup and opens a connection per request."""
    # isolation_level=None leaves transactions to `transaction()` below, so
    # single statements autocommit and nothing holds a lock between calls.
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        if migrate:
            _migrate(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migrate(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute(NOTIFIED_SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(notified_offers)")}
        for name, sql_type in ADDED_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE notified_offers ADD COLUMN {name} {sql_type}")
        # Backfill rows written before this column existed - watchlist_summary and
        # angebote_summary were always the same string at every call site, so this
        # is exact, not a guess. Without it, those old rows stay invisible to
        # prune_expired's staleness check forever (it only looks at non-NULL
        # angebote_summary), leaking their Angebote to-do items permanently.
        conn.execute(
            "UPDATE notified_offers SET angebote_summary = watchlist_summary "
            "WHERE angebote_summary IS NULL"
        )
        if version >= SCHEMA_VERSION:
            return
        for statement in V2_SCHEMA:
            conn.execute(statement)
        # v1 took valid_from as the UTC date of Marktguru's start instant, which is
        # local midnight and so 22:00 or 23:00 UTC on the day before. v2 reads both
        # ends in local time; shifting the v1 rows keeps their dedup keys matching
        # the same offers, so nothing already pushed is pushed again.
        shifted = conn.execute("UPDATE notified_offers SET valid_from = date(valid_from, '+1 day')").rowcount
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        log.info("Migrated store from schema v%d to v%d (%d dedup row(s) re-dated)", version, SCHEMA_VERSION, shifted)


# --- notified_offers: push dedup, written only by the watchlist run ---


def already_notified(conn: sqlite3.Connection, offer: Offer, watchlist_summary: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM notified_offers WHERE offer_id = ? AND valid_from = ? AND valid_to = ? AND watchlist_summary = ?",
        (offer.offer_id, offer.valid_from.isoformat(), offer.valid_to.isoformat(), watchlist_summary),
    ).fetchone()
    is_dup = row is not None
    log.info(
        "Dedup check offer_id=%s window=%s..%s item=%r -> %s",
        offer.offer_id, offer.valid_from, offer.valid_to, watchlist_summary,
        "ALREADY_NOTIFIED" if is_dup else "new",
    )
    return is_dup


def record_notified(
    conn: sqlite3.Connection, offer: Offer, watchlist_summary: str, angebote_summary: str | None = None
) -> None:
    """Inserts the dedup row, or refreshes the display details of an existing one
    (which also backfills rows written before those columns existed)."""
    conn.execute(
        "INSERT INTO notified_offers "
        "(offer_id, valid_from, valid_to, watchlist_summary, angebote_summary, title, retailer, price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (offer_id, valid_from, valid_to, watchlist_summary) DO UPDATE SET "
        "title = excluded.title, retailer = excluded.retailer, price = excluded.price",
        (
            offer.offer_id,
            offer.valid_from.isoformat(),
            offer.valid_to.isoformat(),
            watchlist_summary,
            angebote_summary,
            offer.title,
            offer.retailer,
            offer.price,
        ),
    )
    log.info("Recorded dedup row offer_id=%s item=%r", offer.offer_id, watchlist_summary)


def valid_offers(conn: sqlite3.Connection, watchlist_summary: str, today: date) -> list[StoredOffer]:
    """Every recorded offer for this watchlist item that has not ended before
    `today` (offers starting later are included). Rows without display details
    (written before those columns existed) are skipped."""
    rows = conn.execute(
        "SELECT title, retailer, price, valid_from, valid_to FROM notified_offers "
        "WHERE watchlist_summary = ? AND valid_to >= ? AND retailer IS NOT NULL",
        (watchlist_summary, today.isoformat()),
    ).fetchall()
    return [
        StoredOffer(
            title=title,
            retailer=retailer,
            price=price,
            valid_from=date.fromisoformat(valid_from),
            valid_to=date.fromisoformat(valid_to),
        )
        for title, retailer, price, valid_from, valid_to in rows
    ]


def prune_expired(conn: sqlite3.Connection, before: date) -> list[str]:
    """Drop dedup rows for offers whose validity window has fully passed - keeps
    the table from growing forever on a twice-weekly cadence. Returns the
    angebote_summary of every item with NO remaining valid offer (deduped) so
    the caller can remove those items from the Angebote to-do list - one
    to-do item can hold several stores' offers, so it's only stale once all
    of them have expired, not as soon as the first one does."""
    with transaction(conn):
        still_valid = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT angebote_summary FROM notified_offers "
                "WHERE valid_to >= ? AND angebote_summary IS NOT NULL",
                (before.isoformat(),),
            )
        }
        had_expired = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT angebote_summary FROM notified_offers "
                "WHERE valid_to < ? AND angebote_summary IS NOT NULL",
                (before.isoformat(),),
            )
        }
        removed = conn.execute("DELETE FROM notified_offers WHERE valid_to < ?", (before.isoformat(),)).rowcount
    stale_summaries = sorted(had_expired - still_valid)
    log.info(
        "prune_expired(before=%s): removed %d row(s), %d Angebote item(s) fully expired",
        before, removed, len(stale_summaries),
    )
    return stale_summaries


# --- offers and the fetch log ---


def reserve_search(
    conn: sqlite3.Connection, query_norm: str, plz: str, now: datetime, day_start: datetime, budget: int
) -> int | None:
    """Counts the live searches since `day_start` and, if fewer than `budget`,
    records a new one and returns its fetch id. Counting and recording happen
    under one write lock, so concurrent processes can never overrun the budget.
    The row stays ok=0 until finish_fetch, so a failed search still counts."""
    with transaction(conn):
        if searches_since(conn, day_start) >= budget:
            return None
        return conn.execute(
            "INSERT INTO fetches (query_norm, plz, fetched_at) VALUES (?, ?, ?)",
            (query_norm, plz, to_iso(now)),
        ).lastrowid


def searches_since(conn: sqlite3.Connection, since: datetime) -> int:
    return conn.execute("SELECT COUNT(*) FROM fetches WHERE fetched_at >= ?", (to_iso(since),)).fetchone()[0]


def finish_fetch(conn: sqlite3.Connection, fetch_id: int, now: datetime, offers: list[Offer], total_results: int) -> None:
    """Upserts every offer a successful search returned and links them to it."""
    seen = to_iso(now)
    columns = OFFER_COLUMNS + ("first_seen", "last_seen", "raw_json")
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in ("offer_key", "first_seen"))
    with transaction(conn):
        for position, offer in enumerate(offers):
            values = _offer_values(offer) + (seen, seen, json.dumps(offer.raw, ensure_ascii=False) if offer.raw else None)
            conn.execute(
                f"INSERT INTO offers ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
                f"ON CONFLICT (offer_key) DO UPDATE SET {updates}",
                values,
            )
            conn.execute(
                "INSERT OR IGNORE INTO fetch_offers (fetch_id, offer_key, position) VALUES (?, ?, ?)",
                (fetch_id, offer.offer_id, position),
            )
        conn.execute("UPDATE fetches SET ok = 1, total_results = ? WHERE id = ?", (total_results, fetch_id))


def latest_fetch(conn: sqlite3.Connection, query_norm: str, plz: str, since: datetime | None = None) -> Fetch | None:
    """The newest successful search for this query, optionally only one younger than `since`."""
    row = conn.execute(
        "SELECT id, fetched_at FROM fetches WHERE query_norm = ? AND plz = ? AND ok = 1 AND fetched_at > ? "
        "ORDER BY fetched_at DESC, id DESC LIMIT 1",
        (query_norm, plz, to_iso(since) if since else ""),
    ).fetchone()
    return Fetch(id=row[0], fetched_at=datetime.fromisoformat(row[1])) if row else None


def get_offer(conn: sqlite3.Connection, offer_key: str) -> Offer | None:
    row = conn.execute(f"SELECT {', '.join(OFFER_COLUMNS)} FROM offers WHERE offer_key = ?", (offer_key,)).fetchone()
    return _row_to_offer(row) if row else None


def last_fetches(conn: sqlite3.Connection, unfinished_since: datetime) -> tuple[datetime | None, bool | None, datetime | None]:
    """(time of the newest finished live search, whether it succeeded, time of the
    newest successful one), for the service's Marktguru health. A search that has
    not succeeded and started after `unfinished_since` may still be running, so
    it does not count yet."""
    last = conn.execute(
        "SELECT fetched_at, ok FROM fetches WHERE ok = 1 OR fetched_at <= ? ORDER BY fetched_at DESC, id DESC LIMIT 1",
        (to_iso(unfinished_since),),
    ).fetchone()
    ok = conn.execute("SELECT MAX(fetched_at) FROM fetches WHERE ok = 1").fetchone()[0]
    return (
        datetime.fromisoformat(last[0]) if last else None,
        bool(last[1]) if last else None,
        datetime.fromisoformat(ok) if ok else None,
    )


def fetch_offers(conn: sqlite3.Connection, fetch_id: int) -> list[Offer]:
    """The offers a search returned, in Marktguru's order."""
    rows = conn.execute(
        f"SELECT {', '.join('o.' + c for c in OFFER_COLUMNS)} FROM fetch_offers f "
        "JOIN offers o ON o.offer_key = f.offer_key WHERE f.fetch_id = ? ORDER BY f.position",
        (fetch_id,),
    ).fetchall()
    return [_row_to_offer(row) for row in rows]


def _offer_values(offer: Offer) -> tuple:
    return (
        offer.offer_id, offer.retailer, offer.brand, offer.product, offer.description, offer.category,
        offer.category_id, offer.price, offer.old_price, offer.unit_price, offer.unit, offer.volume,
        offer.quantity, int(offer.app_only), offer.valid_from.isoformat(), offer.valid_to.isoformat(),
        offer.image_url, offer.source_url,
    )


def _row_to_offer(row) -> Offer:
    values = dict(zip(OFFER_COLUMNS, row))
    return Offer(
        offer_id=values.pop("offer_key"),
        valid_from=date.fromisoformat(values.pop("valid_from")),
        valid_to=date.fromisoformat(values.pop("valid_to")),
        app_only=bool(values.pop("app_only")),
        **values,
    )


# --- match verdicts and runtime state ---


def record_match(
    conn: sqlite3.Connection, watchlist_item: str, offer_key: str, score: float, decision: str, method: str, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO matches (watchlist_item, offer_key, score, decision, method, decided_at) VALUES (?, ?, ?, ?, ?, ?)",
        (watchlist_item, offer_key, score, decision, method, to_iso(now)),
    )


def matched_offers(conn: sqlite3.Connection, today: date) -> dict[str, list[Offer]]:
    """Per watchlist item, the offers its newest verdict accepted that have not
    ended before `today`. What the watchlist run matched, read without a search."""
    rows = conn.execute(
        f"SELECT m.watchlist_item, {', '.join('o.' + c for c in OFFER_COLUMNS)} FROM matches m "
        "JOIN offers o ON o.offer_key = m.offer_key "
        "WHERE m.id IN (SELECT MAX(id) FROM matches GROUP BY watchlist_item, offer_key) "
        "AND m.decision = 'match' AND o.valid_to >= ? ORDER BY m.watchlist_item",
        (today.isoformat(),),
    ).fetchall()
    matched: dict[str, list[Offer]] = {}
    for item, *offer in rows:
        matched.setdefault(item, []).append(_row_to_offer(offer))
    return matched


def record_watchlist_run(conn: sqlite3.Connection, now: datetime, items: list[str], new_matches: int) -> None:
    """Marks a watchlist run as finished, for the service's status and /healthz."""
    set_kv(conn, LAST_WATCHLIST_RUN_KV, json.dumps({"items": items, "new_matches": new_matches}, ensure_ascii=False), now)


def last_watchlist_run(conn: sqlite3.Connection) -> dict | None:
    """{"finished_at", "items", "new_matches"} of the newest finished watchlist run."""
    row = get_kv(conn, LAST_WATCHLIST_RUN_KV)
    if row is None:
        return None
    value, finished_at = row
    return {"finished_at": finished_at, **json.loads(value)}


def get_kv(conn: sqlite3.Connection, key: str) -> tuple[str, datetime] | None:
    row = conn.execute("SELECT value, updated_at FROM kv WHERE key = ?", (key,)).fetchone()
    return (row[0], datetime.fromisoformat(row[1])) if row else None


def set_kv(conn: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, to_iso(now)),
    )


def prune(conn: sqlite3.Connection, now: datetime) -> None:
    """Enforces the retention of decision 3: offers 12 months after they ended,
    raw_json 30 days after the offer was last fetched, the fetch log 90 days.
    Match verdicts are kept as long as the offers they refer to."""
    with transaction(conn):
        offers = conn.execute(
            "DELETE FROM offers WHERE valid_to < ?", ((local_date(now) - OFFER_RETENTION).isoformat(),)
        ).rowcount
        raw = conn.execute(
            "UPDATE offers SET raw_json = NULL WHERE raw_json IS NOT NULL AND last_seen < ?",
            (to_iso(now - RAW_JSON_RETENTION),),
        ).rowcount
        fetches = conn.execute("DELETE FROM fetches WHERE fetched_at < ?", (to_iso(now - FETCH_LOG_RETENTION),)).rowcount
        matches = conn.execute("DELETE FROM matches WHERE decided_at < ?", (to_iso(now - MATCH_RETENTION),)).rowcount
    log.info("prune: %d offer(s), %d raw_json, %d fetch(es), %d verdict(s) removed", offers, raw, fetches, matches)
