"""Entry point: fetch -> match -> (conditional escalate) -> dedup -> notify.

Runs to completion once per invocation and then exits - meant to be invoked by a
systemd timer or cron job via `docker run --rm`, not run as a long-lived service.
See `deploy/` for an example unit.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import date

import ha_client
import store
from fetch_marktguru import fetch_offers_for_queries
from llm_escalate import escalate
from match import MatchDecision, classify_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("deal_scraper")

PLZ = os.environ["DEAL_SCRAPER_PLZ"]
TODO_ENTITY_ID = os.environ.get("TODO_ENTITY_ID", "todo.lebensmittel_prospekt_tracker")
ANGEBOTE_ENTITY_ID = os.environ.get("ANGEBOTE_ENTITY_ID", "todo.aktuelle_angebote")
NOTIFY_SERVICE = os.environ["NOTIFY_SERVICE"]
DB_PATH = os.environ.get("DB_PATH", "/data/deal-scraper.db")
DASHBOARD_PATH = os.environ.get("DASHBOARD_PATH", "/lovelace/deal-scraper")
OFFERS_PER_QUERY = 40


def format_validity(offer, today: date) -> str:
    """An offer that starts after today gets its start date too, so next week's
    flyer offer doesn't read as available now."""
    if offer.valid_from > today:
        return f"gueltig ab {offer.valid_from.isoformat()} bis {offer.valid_to.isoformat()}"
    return f"gueltig bis {offer.valid_to.isoformat()}"


def format_todo_description(offer, today: date) -> str:
    price = f"{offer.price:.2f} €" if offer.price is not None else "?"
    return f"{price} bei {offer.retailer}, {format_validity(offer, today)}"


def markdown_list(lines) -> str:
    """HA renders to-do descriptions as markdown, where a bare newline is only a
    soft break, so each line goes out as a list item to stay on its own line."""
    return "\n".join(f"- {line}" for line in lines)


def format_watchlist_description(offers, today: date) -> str | None:
    """One line per store for its cheapest offer valid today and one for its
    cheapest upcoming offer; current offers first, each group cheapest first.
    None when no offer is left, which clears the watchlist item's description."""
    cheapest = {}
    for offer in sorted(
        offers, key=lambda o: (o.valid_from > today, o.price is None, o.price or 0.0, o.valid_from, o.retailer)
    ):
        cheapest.setdefault((offer.retailer, offer.valid_from > today), offer)
    if not cheapest:
        return None
    return markdown_list(format_todo_description(offer, today) for offer in cheapest.values())


def format_angebote_line(offer, today: date) -> str:
    """One line per store for the Angebote to-do item's description - keeps the
    product detail (pack size etc., only present in offer.title) alongside the
    price/store/validity, since the item's summary is just the watchlist name."""
    return f"{offer.title} - {format_todo_description(offer, today)}"


def format_push_message(offers, today: date) -> str:
    future_starts = Counter(offer.valid_from for offer in offers if offer.valid_from > today)
    if not future_starts:
        return f"{len(offers)} neue(s) Angebot(e) gefunden - siehe Einkaufsliste."
    starts = ", ".join(f"{count} ab {start.isoformat()}" for start, count in sorted(future_starts.items()))
    return f"{len(offers)} neue(s) Angebot(e) gefunden, davon {starts} - siehe Einkaufsliste."


def sync_watchlist_descriptions(conn, watchlist: list[dict], today: date) -> None:
    """Rewrites every watchlist item's description from its recorded offers that
    are still valid, clearing it once none are left. Items whose description is
    already correct are not written again."""
    for item in watchlist:
        if not item["uid"]:
            continue
        description = format_watchlist_description(store.valid_offers(conn, item["summary"], today), today)
        if description == (item.get("description") or None):
            continue
        try:
            ha_client.update_todo_item_description(TODO_ENTITY_ID, item["uid"], description)
        except Exception:
            log.exception("Todo description update failed for %r, continuing.", item["summary"])
            continue
        item["description"] = description


def run() -> int:
    log.info(
        "Starting run: PLZ=%s TODO_ENTITY_ID=%s ANGEBOTE_ENTITY_ID=%s NOTIFY_SERVICE=%s DB_PATH=%s DASHBOARD_PATH=%s",
        PLZ, TODO_ENTITY_ID, ANGEBOTE_ENTITY_ID, NOTIFY_SERVICE, DB_PATH, DASHBOARD_PATH,
    )
    watchlist = ha_client.get_todo_items(TODO_ENTITY_ID)
    if not watchlist:
        log.info("Watchlist %s is empty, nothing to do.", TODO_ENTITY_ID)
        return 0
    log.info("Watchlist (%d items): %s", len(watchlist), [i["summary"] for i in watchlist])

    today = date.today()
    notified_offers = []
    escalation_count = 0

    with store.open_store(DB_PATH) as conn:
        stale_angebote = store.prune_expired(conn, today)
        for summary in stale_angebote:
            try:
                ha_client.remove_todo_item(ANGEBOTE_ENTITY_ID, summary)
            except Exception:
                log.exception("Angebote list removal failed for %r, continuing.", summary)
        sync_watchlist_descriptions(conn, watchlist, today)

        angebote_by_summary = {i["summary"]: i for i in ha_client.get_todo_items(ANGEBOTE_ENTITY_ID)}

        for idx, item in enumerate(watchlist, start=1):
            item_summary = item["summary"]
            log.info("--- Item %d/%d: %r (uid=%s) ---", idx, len(watchlist), item_summary, item["uid"])
            offers = fetch_offers_for_queries([item_summary], PLZ, limit=OFFERS_PER_QUERY)
            log.info("  %r: %d candidate offers from Marktguru", item_summary, len(offers))

            results = classify_all(item_summary, offers)
            new_offers = []
            for result in results:
                decision = result.decision
                if decision == MatchDecision.UNCERTAIN:
                    escalation_count += 1
                    is_match = escalate(item_summary, result.offer.title)
                elif decision == MatchDecision.MATCH:
                    is_match = True
                else:
                    is_match = False

                if not is_match:
                    continue
                log.info("  MATCH accepted: %r (score=%.0f, decision=%s)", result.offer.title, result.score, decision.value)

                if store.already_notified(conn, result.offer, item_summary):
                    log.info("  SKIP (dedup): %r already notified for item %r", result.offer.title, item_summary)
                    # Refreshes the row's display details for the watchlist description.
                    store.record_notified(conn, result.offer, item_summary, item_summary)
                    continue

                new_offers.append((result.score, result.offer))

            if not new_offers:
                continue

            # Batched into one upsert per item, so dedup rows are only recorded once
            # it succeeds - a failed call leaves nothing recorded and the next run
            # retries all of it, same retry-safety as the old per-offer add_item.
            existing = angebote_by_summary.get(item_summary)
            new_lines = [format_angebote_line(offer, today) for _, offer in new_offers]
            try:
                if existing and existing.get("uid"):
                    prior_lines = [
                        line.removeprefix("- ")
                        for line in (existing.get("description") or "").split("\n")
                        if line.strip()
                    ]
                    ha_client.update_todo_item_description(
                        ANGEBOTE_ENTITY_ID, existing["uid"], markdown_list(prior_lines + new_lines)
                    )
                else:
                    ha_client.add_todo_item(ANGEBOTE_ENTITY_ID, item_summary, markdown_list(new_lines))
            except Exception:
                log.exception(
                    "Angebote list upsert failed for %r, not recording dedup so next run retries.",
                    item_summary,
                )
                continue

            for score, offer in new_offers:
                store.record_notified(conn, offer, item_summary, item_summary)
                notified_offers.append(offer)
                log.info("  NEW MATCH (score=%.0f): %s bei %s", score, offer.title, offer.retailer)

        sync_watchlist_descriptions(conn, watchlist, today)

    if notified_offers:
        try:
            ha_client.notify(
                NOTIFY_SERVICE,
                format_push_message(notified_offers, today),
                title="Prospekt-Check",
                data={"tag": "deal-scraper", "url": DASHBOARD_PATH},
            )
        except Exception:
            log.exception("Summary notify failed.")

    log.info(
        "Done. %d new match(es), %d escalated to the local model.",
        len(notified_offers),
        escalation_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
