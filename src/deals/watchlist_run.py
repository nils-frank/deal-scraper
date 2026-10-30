"""The watchlist run (`deals run-watchlist`): fetch -> match -> (conditional
escalate) -> dedup -> notify.

Runs to completion once per invocation and then exits - meant to be invoked by a
systemd timer or cron job via `docker run --rm`, not run as a long-lived service.
See `deploy/` for an example unit. Its searches go through the shared Marktguru
client, so they are cached, stored and counted against the same daily budget as
every other caller.
"""
from __future__ import annotations

import logging
import os
from datetime import date

from deals import ha, marktguru, query, store
from deals.llm_escalate import escalate
from deals.match import MatchDecision, MatchMethod, classify_all
from deals.schema import local_date, utcnow

log = logging.getLogger(__name__)

TODO_ENTITY_ID = os.environ.get("TODO_ENTITY_ID", "todo.lebensmittel_prospekt_tracker")
ANGEBOTE_ENTITY_ID = os.environ.get("ANGEBOTE_ENTITY_ID", "todo.aktuelle_angebote")
DASHBOARD_PATH = os.environ.get("DASHBOARD_PATH", "/lovelace/deal-scraper")
# Where tapping the push leads. A full URL ("https://<ha-host>/lovelace/...") rather
# than the relative path works around the Companion App not navigating on tap.
NOTIFY_URL = os.environ.get("NOTIFY_URL", DASHBOARD_PATH)
# Offers named in one push; the rest are counted.
PUSH_OFFERS = 5
# Action IDs of the push buttons, handled by a Home Assistant automation on
# mobile_app_notification_action that calls POST /api/shopping-list or /api/mute.
ADD_ACTION = "DEALS_ADD"
MUTE_ACTION = "DEALS_MUTE"


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


def push_order(offers, today: date) -> list:
    """Current offers first, then upcoming ones, each cheapest first."""
    return sorted(offers, key=lambda o: (o.valid_from > today, o.price is None, o.price or 0.0, o.valid_from, o.retailer))


def build_push(item_summary: str, offers, today: date) -> dict:
    """The push for one watchlist item's new offers: one line per offer with its
    validity span, and the two actions for the first of them, the cheapest
    current offer or, when none runs yet, the cheapest upcoming one."""
    ordered = push_order(offers, today)
    lines = [query.format_push_line(offer, today) for offer in ordered[:PUSH_OFFERS]]
    if len(ordered) > PUSH_OFFERS:
        lines.append(f"und {len(ordered) - PUSH_OFFERS} weitere")
    key = ordered[0].offer_id
    return {
        "title": f"Angebot: {item_summary}",
        "message": "\n".join(lines),
        "data": {
            # One notification per item, so a later push for it replaces this one.
            "tag": f"deal-scraper-{item_summary}",
            "group": "deal-scraper",
            "url": NOTIFY_URL,
            "clickAction": NOTIFY_URL,
            "actions": [
                {"action": f"{ADD_ACTION}:{key}", "title": "Auf Einkaufsliste"},
                {"action": f"{MUTE_ACTION}:{key}", "title": "Nicht mehr melden"},
            ],
        },
    }


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
            ha.update_todo_item_description(TODO_ENTITY_ID, item["uid"], description)
        except Exception:
            log.exception("Todo description update failed for %r, continuing.", item["summary"])
            continue
        item["description"] = description


def run() -> int:
    plz = os.environ["DEAL_SCRAPER_PLZ"]
    notify_service = os.environ["NOTIFY_SERVICE"]
    db_path = os.environ.get("DB_PATH", store.DEFAULT_DB_PATH)
    log.info(
        "Starting run: PLZ=%s TODO_ENTITY_ID=%s ANGEBOTE_ENTITY_ID=%s NOTIFY_SERVICE=%s DB_PATH=%s NOTIFY_URL=%s",
        plz, TODO_ENTITY_ID, ANGEBOTE_ENTITY_ID, notify_service, db_path, NOTIFY_URL,
    )
    watchlist = ha.get_todo_items(TODO_ENTITY_ID)
    if not watchlist:
        log.info("Watchlist %s is empty, nothing to do.", TODO_ENTITY_ID)
        with store.open_store(db_path) as conn:
            store.record_watchlist_run(conn, utcnow(), [], 0)
        return 0
    log.info("Watchlist (%d items): %s", len(watchlist), [i["summary"] for i in watchlist])

    now = utcnow()
    today = local_date(now)
    notified_offers = []
    pushes = []
    escalation_count = 0

    with store.open_store(db_path) as conn:
        store.prune(conn, now)
        client = marktguru.Client(conn, plz, clock=utcnow)
        stale_angebote = store.prune_expired(conn, today)
        for summary in stale_angebote:
            try:
                ha.remove_todo_item(ANGEBOTE_ENTITY_ID, summary)
            except Exception:
                log.exception("Angebote list removal failed for %r, continuing.", summary)
        sync_watchlist_descriptions(conn, watchlist, today)

        angebote_by_summary = {i["summary"]: i for i in ha.get_todo_items(ANGEBOTE_ENTITY_ID)}

        for idx, item in enumerate(watchlist, start=1):
            item_summary = item["summary"]
            log.info("--- Item %d/%d: %r (uid=%s) ---", idx, len(watchlist), item_summary, item["uid"])
            search = client.search_narrowing(item_summary)
            offers = [offer for offer in search.offers if offer.valid_to >= today]
            log.info(
                "  %r: %d candidate offers from Marktguru (%s, fetched %s)",
                item_summary, len(offers), search.source, search.fetched_at,
            )

            results = classify_all(item_summary, offers)
            new_offers = []
            for result in results:
                decision, method = result.decision, result.method
                if decision == MatchDecision.UNCERTAIN:
                    escalation_count += 1
                    decision = MatchDecision.MATCH if escalate(item_summary, result.offer.title) else MatchDecision.NO_MATCH
                    method = MatchMethod.LLM
                store.record_match(conn, item_summary, result.offer.offer_id, result.score, decision.value, method.value, now)

                if decision != MatchDecision.MATCH:
                    continue
                log.info("  MATCH accepted: %r (score=%.0f, method=%s)", result.offer.title, result.score, method.value)

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
                    ha.update_todo_item_description(
                        ANGEBOTE_ENTITY_ID, existing["uid"], markdown_list(prior_lines + new_lines)
                    )
                else:
                    ha.add_todo_item(ANGEBOTE_ENTITY_ID, item_summary, markdown_list(new_lines))
            except Exception:
                log.exception(
                    "Angebote list upsert failed for %r, not recording dedup so next run retries.",
                    item_summary,
                )
                continue

            to_push = []
            for score, offer in new_offers:
                store.record_notified(conn, offer, item_summary, item_summary)
                notified_offers.append(offer)
                log.info("  NEW MATCH (score=%.0f): %s bei %s", score, offer.title, offer.retailer)
                if store.is_muted(conn, offer.offer_id):
                    log.info("  MUTED: %r is not pushed", offer.title)
                else:
                    to_push.append(offer)
            if to_push:
                pushes.append(build_push(item_summary, to_push, today))

        sync_watchlist_descriptions(conn, watchlist, today)
        store.record_watchlist_run(conn, utcnow(), [i["summary"] for i in watchlist], len(notified_offers))

    for push in pushes:
        try:
            ha.notify(notify_service, push["message"], title=push["title"], data=push["data"])
        except Exception:
            log.exception("Notify failed for %r, continuing.", push["title"])

    log.info(
        "Done. %d new match(es), %d escalated to the local model.",
        len(notified_offers),
        escalation_count,
    )
    return 0

