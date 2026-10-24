"""Marktguru fetcher.

Reference implementation: https://github.com/benmillerat/openclaw-supermarket-deals
Marktguru's internal search API needs no registration: an `apiKey` + `clientKey`
pair is embedded in a `<script type="application/json">` block on the homepage and
accepted as request headers. Keys are re-extracted every run (the container is
started with --rm and torn down afterwards, so there is nothing worth caching them
in between runs).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Optional

import requests

from schema import Offer

log = logging.getLogger(__name__)

HOME_URL = "https://www.marktguru.de"
# Same image URL the Marktguru web frontend uses for an offer's thumbnail; it is
# derived from the offer id, so it costs no extra request.
IMAGE_URL_TEMPLATE = "https://cdn.marktguru.de/api/v1/offers/{offer_id}/images/default/0/small.webp"
SEARCH_ENDPOINT = "https://api.marktguru.de/api/v1/offers/search"
REQUEST_TIMEOUT = 15

# Offer "industries" (Marktguru's own retailer-category tagging, from
# /api/v1/industries) that actually sell groceries. Without this, a
# single-brand watchlist entry like "Philadelphia" fuzzy-matches 100% against
# any offer carrying that brand name regardless of what's being sold - e.g. a
# "WMF Besteck-Set Philadelphia" cutlery set at XXXLutz (industry
# "Möbelhaus"). Retailers here (Lidl, EDEKA, Getränke Hoffmann, ...) can still
# run non-food weekly specials, but that's a rarer, in-scope-store edge case
# distinct from the wrong-store false positive actually observed.
GROCERY_INDUSTRIES = {"Supermarkt", "Discounter", "Getränkemarkt"}


class MarktguruError(RuntimeError):
    pass


def _extract_keys(html: str) -> tuple[str, str]:
    for match in re.finditer(
        r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', html, re.S | re.I
    ):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        config = parsed.get("config") if isinstance(parsed, dict) else None
        if isinstance(config, dict) and config.get("apiKey") and config.get("clientKey"):
            return config["apiKey"], config["clientKey"]
    raise MarktguruError("could not extract apiKey/clientKey from Marktguru homepage")


def _fetch_keys(session: requests.Session) -> tuple[str, str]:
    resp = session.get(HOME_URL, headers={"Accept": "text/html"}, timeout=REQUEST_TIMEOUT)
    log.info("GET %s -> %d (%d bytes)", HOME_URL, resp.status_code, len(resp.content))
    resp.raise_for_status()
    api_key, client_key = _extract_keys(resp.text)
    log.info("Extracted Marktguru keys: apiKey=%s... clientKey=%s...", api_key[:6], client_key[:6])
    return api_key, client_key


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _offer_to_normalized(raw: dict, plz: str) -> Optional[Offer]:
    offer_id = raw.get("id")
    if offer_id is None:
        return None
    offer_id = str(offer_id)

    industries = {i.get("name") for i in raw.get("industries") or []}
    if industries and not (industries & GROCERY_INDUSTRIES):
        log.debug("Dropping offer_id=%s: non-grocery industry %r", offer_id, industries)
        return None

    product = raw.get("product") or {}
    brand_name = (raw.get("brand") or {}).get("name")
    product_name = product.get("name")
    description = raw.get("description")

    # product.name alone is often a generic bucket ("Cola", "Cola XXL"); brand and the
    # flyer description carry the specifics ("Fanta/Sprite/MezzoMix Versch. Sorten") that
    # the fuzzy matcher and LLM escalation actually need to tell offers apart.
    title_parts = [p.strip() for p in (brand_name, product_name, description) if p and p.strip()]
    title = " - ".join(dict.fromkeys(title_parts))  # de-dup identical parts, keep order
    if not title:
        return None

    advertisers = raw.get("advertisers") or []
    retailer = (advertisers[0].get("name") if advertisers else None) or "unknown"

    price = raw.get("price") if isinstance(raw.get("price"), (int, float)) else None
    unit_price = raw.get("referencePrice") if isinstance(raw.get("referencePrice"), (int, float)) else None
    unit = (raw.get("unit") or {}).get("shortName")

    validity = raw.get("validityDates") or []
    valid_from = _parse_date(validity[0].get("from")) if validity else None
    valid_to = _parse_date(validity[0].get("to")) if validity else None
    if valid_from is None or valid_to is None:
        # Undated offers can't be matched against a validity window later - skip.
        log.debug("Dropping offer_id=%s title=%r: no valid validityDates (%r)", offer_id, title, validity)
        return None

    has_image = offer_id.isdigit() and (raw.get("images") or {}).get("count", 1) != 0

    return Offer(
        title=title,
        brand=brand_name,
        price=round(float(price), 2) if price is not None else None,
        unit_price=round(float(unit_price), 2) if unit_price is not None else None,
        unit=unit,
        retailer=retailer,
        plz=plz,
        valid_from=valid_from,
        valid_to=valid_to,
        image_url=IMAGE_URL_TEMPLATE.format(offer_id=offer_id) if has_image else None,
        source_url=f"https://www.marktguru.de/offers/{offer_id}" if offer_id.isdigit() else None,
        offer_id=f"marktguru:{offer_id}",
    )


def search_offers(
    session: requests.Session, api_key: str, client_key: str, query: str, plz: str, limit: int = 40
) -> list[Offer]:
    params = {"as": "web", "limit": str(limit), "q": query, "zipCode": plz}
    log.info("GET %s params=%s", SEARCH_ENDPOINT, params)
    resp = session.get(
        SEARCH_ENDPOINT,
        params=params,
        headers={"x-apikey": api_key, "x-clientkey": client_key, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    log.info("  -> %d (%d bytes)", resp.status_code, len(resp.content))
    if resp.status_code in (401, 403):
        raise MarktguruError(f"stale keys (HTTP {resp.status_code})")
    resp.raise_for_status()
    body = resp.json()
    results = body.get("results")
    if not isinstance(results, list):
        raise MarktguruError("unexpected response shape: no 'results' array")

    offers = []
    dropped = 0
    for raw in results:
        offer = _offer_to_normalized(raw, plz)
        if offer is not None:
            offers.append(offer)
        else:
            dropped += 1
    log.info(
        "search_offers(query=%r): %d raw result(s), %d normalized, %d dropped",
        query, len(results), len(offers), dropped,
    )
    return offers


def fetch_offers_for_queries(queries: list[str], plz: str, limit: int = 40) -> list[Offer]:
    """One Marktguru search per query string, merged and de-duplicated by offer_id."""
    session = requests.Session()
    session.headers["User-Agent"] = "deal-scraper/1.0 (+homelab)"

    api_key, client_key = _fetch_keys(session)

    seen: dict[str, Offer] = {}
    for query in queries:
        try:
            offers = search_offers(session, api_key, client_key, query, plz, limit)
        except MarktguruError as exc:
            if "stale keys" in str(exc):
                log.warning("Marktguru keys rejected, refreshing once: %s", exc)
                api_key, client_key = _fetch_keys(session)
                offers = search_offers(session, api_key, client_key, query, plz, limit)
            else:
                log.warning("Marktguru search failed for %r: %s", query, exc)
                continue
        except requests.RequestException as exc:
            log.warning("Marktguru search failed for %r: %s", query, exc)
            continue

        # Marktguru's search only matches brand/product-name fields, not the flyer
        # description - a watchlist entry with extra descriptive words ("Philadelphia
        # Nature", "Alpro Not M*lk") that aren't in the offer's title can return 0
        # results even though the item is on offer under a shorter name. Retry once
        # with just the first word (typically the brand) and let match.py's fuzzy
        # scorer + LLM escalation filter the wider result set for precision.
        words = query.split()
        if not offers and len(words) > 1:
            narrowed = words[0]
            log.info("No offers for %r, retrying with narrowed query %r", query, narrowed)
            try:
                offers = search_offers(session, api_key, client_key, narrowed, plz, limit)
            except (MarktguruError, requests.RequestException) as exc:
                log.warning("Narrowed search failed for %r: %s", narrowed, exc)
                offers = []

        for offer in offers:
            seen[offer.offer_id] = offer

    return list(seen.values())
