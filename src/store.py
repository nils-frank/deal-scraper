"""SQLite dedup store, keyed on offer ID + validity window.

Lives on a local-path PVC (see k8s/deal-scraper/deal-scraper.yaml). Same pattern
as Mealie's SQLite-on-PVC deployment - see DESIGN.md.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date

from schema import Offer

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notified_offers (
    offer_id    TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    watchlist_summary TEXT NOT NULL,
    notified_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (offer_id, valid_from, valid_to, watchlist_summary)
);
"""


@contextmanager
def open_store(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)
        try:
            conn.execute("ALTER TABLE notified_offers ADD COLUMN angebote_summary TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
        # Backfill rows written before this column existed - watchlist_summary and
        # angebote_summary were always the same string at every call site, so this
        # is exact, not a guess. Without it, those old rows stay invisible to
        # prune_expired's staleness check forever (it only looks at non-NULL
        # angebote_summary), leaking their Angebote to-do items permanently.
        conn.execute(
            "UPDATE notified_offers SET angebote_summary = watchlist_summary "
            "WHERE angebote_summary IS NULL"
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


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
    conn.execute(
        "INSERT OR IGNORE INTO notified_offers "
        "(offer_id, valid_from, valid_to, watchlist_summary, angebote_summary) VALUES (?, ?, ?, ?, ?)",
        (
            offer.offer_id,
            offer.valid_from.isoformat(),
            offer.valid_to.isoformat(),
            watchlist_summary,
            angebote_summary,
        ),
    )
    conn.commit()
    log.info("Recorded dedup row offer_id=%s item=%r", offer.offer_id, watchlist_summary)


def prune_expired(conn: sqlite3.Connection, before: date) -> list[str]:
    """Drop dedup rows for offers whose validity window has fully passed - keeps
    the table from growing forever on a twice-weekly cadence. Returns the
    angebote_summary of every item with NO remaining valid offer (deduped) so
    the caller can remove those items from the Angebote to-do list - one
    to-do item can hold several stores' offers, so it's only stale once all
    of them have expired, not as soon as the first one does."""
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
    stale_summaries = sorted(had_expired - still_valid)
    cur = conn.execute("DELETE FROM notified_offers WHERE valid_to < ?", (before.isoformat(),))
    conn.commit()
    log.info(
        "prune_expired(before=%s): removed %d row(s), %d Angebote item(s) fully expired",
        before, cur.rowcount, len(stale_summaries),
    )
    return stale_summaries
