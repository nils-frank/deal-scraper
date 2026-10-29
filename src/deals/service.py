"""The deal service core: what the JSON API and the MCP server answer.

`api.py` and `mcp_server.py` are thin wrappers over DealService, so Home
Assistant and an agent get the same answer to the same question. Every call
opens its own connection to the store file the watchlist run uses, so the
service and the batch share one Marktguru cache and one daily search budget.
Read calls never write notified_offers or the match log.

The write calls change Home Assistant to-do lists through `ha`, with the same
HA_TOKEN the batch uses: the watchlist and the shopping list.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import requests

from deals import ha, marktguru, query, store, watchlist_run
from deals.schema import local_date, utcnow

log = logging.getLogger(__name__)

TOKEN_ENV = "DEAL_SERVICE_TOKEN"
# `openssl rand -hex 32` prints 64 characters.
MIN_TOKEN_LENGTH = 32
SHOPPING_LIST_ENTITY_ID = "todo.mealie_lebensmittel"
MAX_LIMIT = 50


class ConfigError(RuntimeError):
    pass


class ServiceError(Exception):
    """A request the service cannot answer, with the HTTP status the JSON API gives it."""

    status = 400


class InvalidRequest(ServiceError):
    status = 400


class NotFound(ServiceError):
    status = 404


class UpstreamError(ServiceError):
    status = 502


@dataclass(frozen=True)
class Settings:
    plz: str
    token: str
    db_path: str = store.DEFAULT_DB_PATH
    watchlist_entity: str = watchlist_run.TODO_ENTITY_ID
    shopping_list_entity: str = SHOPPING_LIST_ENTITY_ID

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Settings:
        """Refuses to configure a service without its bearer token. HA_TOKEN is
        required too, since the write tools need it."""
        missing = [name for name in ("DEAL_SCRAPER_PLZ", TOKEN_ENV, "HA_TOKEN") if not environ.get(name)]
        if missing:
            raise ConfigError(f"set {', '.join(missing)} to start the deal service")
        token = environ[TOKEN_ENV].strip()
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"{TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters, e.g. from `openssl rand -hex 32`")
        return cls(
            plz=environ["DEAL_SCRAPER_PLZ"],
            token=token,
            db_path=environ.get("DB_PATH", store.DEFAULT_DB_PATH),
            watchlist_entity=environ.get("TODO_ENTITY_ID", watchlist_run.TODO_ENTITY_ID),
            shopping_list_entity=environ.get("SHOPPING_LIST_ENTITY_ID", SHOPPING_LIST_ENTITY_ID),
        )


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


def _check_when(when: str) -> None:
    if when not in query.WHEN_CHOICES:
        raise InvalidRequest(f"when must be one of {', '.join(query.WHEN_CHOICES)}, not {when!r}")


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _find_item(items: list[dict], summary: str) -> Optional[dict]:
    """The to-do item with this summary, ignoring case and spacing."""
    wanted = _normalized(summary)
    return next((item for item in items if _normalized(item["summary"]) == wanted), None)


def _ha_failure(exc: requests.RequestException) -> str:
    """A short reason; requests' own messages carry the Home Assistant URL."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection failed"
    return "request failed"


class DealService:
    def __init__(self, settings: Settings, *, clock: Callable[[], datetime] = utcnow):
        self.settings = settings
        self.clock = clock

    def migrate(self) -> None:
        """Brings the store to the current schema once, at startup."""
        with store.open_store(self.settings.db_path):
            pass

    @contextmanager
    def _store(self):
        with store.open_store(self.settings.db_path, migrate=False) as conn:
            yield conn

    def _client(self, conn) -> marktguru.Client:
        return marktguru.Client(conn, self.settings.plz, clock=self.clock)

    # --- reads ---

    def find_offers(
        self, q: str, when: str = "now", stores: Optional[list[str]] = None, limit: int = query.DEFAULT_LIMIT
    ) -> dict:
        """`deals find` as JSON, plus the short German answer for Home Assistant."""
        q = " ".join((q or "").split())
        if not q:
            raise InvalidRequest("the query must not be empty")
        _check_when(when)
        if not 1 <= limit <= MAX_LIMIT:
            raise InvalidRequest(f"limit must be between 1 and {MAX_LIMIT}")
        with self._store() as conn:
            result = query.find_offers(self._client(conn), q, when, stores=stores or None, limit=limit)
        return {**result.to_dict(), "speech": query.format_speech(result, self.clock())}

    def watchlist_deals(self, when: str = "now") -> dict:
        """What the last watchlist run matched, per watchlist item, from the store
        alone: no Marktguru search. Items without a matching offer for `when` are
        listed by name only."""
        _check_when(when)
        now = self.clock()
        today = local_date(now)
        with self._store() as conn:
            run = store.last_watchlist_run(conn)
            matched = store.matched_offers(conn, today)
        # Before the first recorded run, every item with a match counts.
        items = run["items"] if run else sorted(matched)
        with_offers, without_offers = [], []
        for item in items:
            current, upcoming = query.split_by_when(matched.get(item, []), when, today)
            if not current and not upcoming:
                without_offers.append(item)
                continue
            with_offers.append({
                "item": item,
                "current": [query.group_to_dict(g, today) for g in query.group_offers(current)],
                "upcoming": [query.group_to_dict(g, today) for g in query.group_offers(upcoming)],
            })
        return {
            "when": when,
            "today": today.isoformat(),
            "reference_day": query.reference_day(today).isoformat(),
            "items": with_offers,
            "without_offers": without_offers,
            "last_watchlist_run": self._run_dict(run, now),
        }

    def status(self) -> dict:
        """The last watchlist run, the search budget used today and Marktguru's health."""
        now = self.clock()
        with self._store() as conn:
            run = store.last_watchlist_run(conn)
            client = self._client(conn)
            left = client.budget_left()
            last_search_at, last_ok, last_success_at = store.last_fetches(conn)
        if last_ok is None:
            health = "unknown"
        else:
            health = "ok" if last_ok else "failing"
        return {
            "now": now.isoformat(),
            "last_watchlist_run": self._run_dict(run, now),
            "budget": {"daily": client.daily_budget, "used_today": client.daily_budget - left, "left": left},
            "marktguru": {
                "health": health,
                "last_search_at": _iso(last_search_at),
                "last_success_at": _iso(last_success_at),
            },
        }

    def health(self) -> tuple[bool, dict]:
        """For the open /healthz: whether the store answers, and how old the last
        watchlist run is. Says nothing about offers or configuration."""
        now = self.clock()
        try:
            with self._store() as conn:
                run = store.last_watchlist_run(conn)
        except sqlite3.Error as exc:
            log.warning("Health check: store unreachable: %s", exc)
            return False, {"status": "error", "store": "unreachable"}
        return True, {
            "status": "ok",
            "store": "ok",
            "last_watchlist_run": _iso(run["finished_at"]) if run else None,
            "last_watchlist_run_age_seconds": int((now - run["finished_at"]).total_seconds()) if run else None,
        }

    @staticmethod
    def _run_dict(run: Optional[dict], now: datetime) -> Optional[dict]:
        if run is None:
            return None
        return {
            "finished_at": run["finished_at"].isoformat(),
            "age_hours": round((now - run["finished_at"]).total_seconds() / 3600, 1),
            "items": len(run["items"]),
            "new_matches": run["new_matches"],
        }

    # --- writes ---

    def watchlist_add(self, item: str) -> dict:
        item = " ".join((item or "").split())
        if not item:
            raise InvalidRequest("the item must not be empty")
        entity = self.settings.watchlist_entity
        existing = _find_item(self._ha(ha.get_todo_items, entity), item)
        if existing:
            return {"added": False, "item": existing["summary"], "list": entity, "reason": "already on the watchlist"}
        self._ha(ha.add_todo_item, entity, item)
        return {"added": True, "item": item, "list": entity, "note": "The next watchlist run searches offers for it."}

    def watchlist_remove(self, item: str) -> dict:
        entity = self.settings.watchlist_entity
        items = self._ha(ha.get_todo_items, entity)
        existing = _find_item(items, item or "")
        if existing is None:
            names = ", ".join(i["summary"] for i in items) or "nothing"
            raise NotFound(f"{item!r} is not on the watchlist; it has {names}")
        self._ha(ha.remove_todo_item, entity, existing["summary"])
        return {"removed": True, "item": existing["summary"], "list": entity}

    def add_to_shopping_list(self, offer_key: str) -> dict:
        """Puts "Bergwiese Feine Butter 1,29 € (Markt C, bis Sa)" on the shopping list,
        for an offer a find_offers or watchlist_deals answer named."""
        today = local_date(self.clock())
        with self._store() as conn:
            offer = store.get_offer(conn, (offer_key or "").strip())
        if offer is None:
            raise NotFound(f"no stored offer {offer_key!r}; pass an offer_key from find_offers or watchlist_deals")
        if offer.valid_to < today:
            raise InvalidRequest(f"this offer ended on {offer.valid_to.isoformat()}")
        text = query.format_shopping_item(offer, today)
        entity = self.settings.shopping_list_entity
        offer_dict = {**offer.to_dict(), "validity": query.validity_span(offer, today, "de")}
        if _find_item(self._ha(ha.get_todo_items, entity), text):
            return {"added": False, "item": text, "list": entity, "reason": "already on the shopping list", "offer": offer_dict}
        self._ha(ha.add_todo_item, entity, text)
        return {"added": True, "item": text, "list": entity, "offer": offer_dict}

    @staticmethod
    def _ha(call, *args):
        try:
            return call(*args)
        except requests.RequestException as exc:
            log.warning("Home Assistant call %s failed: %s", call.__name__, exc)
            raise UpstreamError(f"Home Assistant did not answer ({_ha_failure(exc)})") from exc
