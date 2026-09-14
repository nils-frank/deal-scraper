"""Entry point: fetch -> match -> (conditional escalate) -> dedup -> notify.

Runs to completion once per invocation and then exits - meant to be invoked by a
systemd timer or cron job via `docker run --rm`, not run as a long-lived service.
See `deploy/` for an example unit.
"""
from __future__ import annotations

import logging
import os
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


def format_todo_description(offer) -> str:
    price = f"{offer.price:.2f} €" if offer.price is not None else "?"
    return f"{price} bei {offer.retailer}, gueltig bis {offer.valid_to.isoformat()}"


def format_angebote_line(offer) -> str:
    """One line per store for the Angebote to-do item's description - keeps the
    product detail (pack size etc., only present in offer.title) alongside the
    price/store/validity, since the item's summary is just the watchlist name."""
    return f"{offer.title} - {format_todo_description(offer)}"


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

    new_match_count = 0
    escalation_count = 0

    with store.open_store(DB_PATH) as conn:
        stale_angebote = store.prune_expired(conn, date.today())
        for summary in stale_angebote:
            try:
                ha_client.remove_todo_item(ANGEBOTE_ENTITY_ID, summary)
            except Exception:
                log.exception("Angebote list removal failed for %r, continuing.", summary)

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
                    continue

                offer = result.offer
                if item["uid"]:
                    try:
                        ha_client.update_todo_item_description(
                            TODO_ENTITY_ID, item["uid"], format_todo_description(offer)
                        )
                    except Exception:
                        log.exception("Todo description update failed for %r, continuing.", item_summary)

                new_offers.append((result.score, offer))

            if not new_offers:
                continue

            # Batched into one upsert per item, so dedup rows are only recorded once
            # it succeeds - a failed call leaves nothing recorded and the next run
            # retries all of it, same retry-safety as the old per-offer add_item.
            existing = angebote_by_summary.get(item_summary)
            new_lines = [format_angebote_line(offer) for _, offer in new_offers]
            try:
                if existing and existing.get("uid"):
                    prior_lines = [
                        line for line in (existing.get("description") or "").split("\n") if line.strip()
                    ]
                    ha_client.update_todo_item_description(
                        ANGEBOTE_ENTITY_ID, existing["uid"], "\n".join(prior_lines + new_lines)
                    )
                else:
                    ha_client.add_todo_item(ANGEBOTE_ENTITY_ID, item_summary, "\n".join(new_lines))
            except Exception:
                log.exception(
                    "Angebote list upsert failed for %r, not recording dedup so next run retries.",
                    item_summary,
                )
                continue

            for score, offer in new_offers:
                store.record_notified(conn, offer, item_summary, item_summary)
                new_match_count += 1
                log.info("  NEW MATCH (score=%.0f): %s bei %s", score, offer.title, offer.retailer)

    if new_match_count:
        message = f"{new_match_count} neue(s) Angebot(e) gefunden - siehe Einkaufsliste."
        try:
            ha_client.notify(
                NOTIFY_SERVICE,
                message,
                title="Prospekt-Check",
                data={"tag": "deal-scraper", "url": DASHBOARD_PATH},
            )
        except Exception:
            log.exception("Summary notify failed.")

    log.info(
        "Done. %d new match(es), %d escalated to the local model.",
        new_match_count,
        escalation_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
