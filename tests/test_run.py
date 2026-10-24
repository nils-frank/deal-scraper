"""main.run() end to end, with Home Assistant and Marktguru replaced by fakes."""
from __future__ import annotations

from datetime import date

import pytest

import ha_client
import main
import store
from conftest import TODAY, make_offer


class FakeDate(date):
    @classmethod
    def today(cls):
        return TODAY


class FakeHomeAssistant:
    def __init__(self, watchlist: list[dict]):
        self.lists = {main.TODO_ENTITY_ID: watchlist, main.ANGEBOTE_ENTITY_ID: []}
        self.description_updates: list[tuple[str, str, str | None]] = []
        self.pushes: list[str] = []

    def get_todo_items(self, entity_id):
        return [dict(item) for item in self.lists[entity_id]]

    def update_todo_item_description(self, entity_id, uid, description):
        self.description_updates.append((entity_id, uid, description))
        for item in self.lists[entity_id]:
            if item["uid"] == uid:
                item["description"] = description

    def add_todo_item(self, entity_id, summary, description=None):
        uid = f"{entity_id}-{len(self.lists[entity_id])}"
        self.lists[entity_id].append({"uid": uid, "summary": summary, "description": description})

    def remove_todo_item(self, entity_id, summary):
        self.lists[entity_id] = [i for i in self.lists[entity_id] if i["summary"] != summary]

    def notify(self, service, message, title=None, data=None):
        self.pushes.append(message)

    def watchlist_description(self, uid):
        return next(i["description"] for i in self.lists[main.TODO_ENTITY_ID] if i["uid"] == uid)

    def watchlist_updates(self):
        return [u for u in self.description_updates if u[0] == main.TODO_ENTITY_ID]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Returns a function that installs a fake HA with the given watchlist and a
    fake Marktguru serving offers[summary]."""
    db_path = str(tmp_path / "run.db")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "date", FakeDate)
    offers: dict[str, list] = {}
    monkeypatch.setattr(main, "fetch_offers_for_queries", lambda queries, plz, limit: list(offers.get(queries[0], [])))
    monkeypatch.setattr(main, "escalate", lambda summary, title: False)

    def install(watchlist):
        ha = FakeHomeAssistant(watchlist)
        for name in ("get_todo_items", "update_todo_item_description", "add_todo_item", "remove_todo_item", "notify"):
            monkeypatch.setattr(ha_client, name, getattr(ha, name))
        return ha

    install.offers = offers
    install.db_path = db_path
    return install


def test_expired_offer_is_cleared_from_watchlist(env):
    expired = make_offer(title="Tomaten - Passiert", retailer="Markt K", price=0.99,
                         valid_from=date(2026, 10, 12), valid_to=date(2026, 10, 17))
    with store.open_store(env.db_path) as conn:
        store.record_notified(conn, expired, "Tomaten", "Tomaten")
    ha = env([{"uid": "w1", "summary": "Tomaten", "description": "0.99 € bei Markt K, gueltig bis 2026-10-17"}])
    ha.lists[main.ANGEBOTE_ENTITY_ID].append({"uid": "a1", "summary": "Tomaten", "description": "old line"})

    assert main.run() == 0

    assert ha.watchlist_description("w1") is None
    assert ha.watchlist_updates() == [(main.TODO_ENTITY_ID, "w1", None)]
    assert ha.lists[main.ANGEBOTE_ENTITY_ID] == []
    assert ha.pushes == []


def test_all_stores_listed_current_first_and_future_offer_marked(env):
    ha = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.offers["Frischkaese"] = [
        make_offer(offer_id="1", title="Frischkaese Natur", retailer="Markt B", price=1.11),
        make_offer(offer_id="2", title="Frischkaese Natur", retailer="Markt A", price=1.11),
        make_offer(offer_id="3", title="Frischkaese Natur", retailer="Markt C", price=0.99,
                   valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31)),
        make_offer(offer_id="4", title="Fahrradhelm Sport", retailer="Markt D", price=19.99),
    ]

    main.run()

    assert ha.watchlist_description("w1") == (
        "- 1.11 € bei Markt A, gueltig bis 2026-10-24\n"
        "- 1.11 € bei Markt B, gueltig bis 2026-10-24\n"
        "- 0.99 € bei Markt C, gueltig ab 2026-10-26 bis 2026-10-31"
    )
    angebote = ha.lists[main.ANGEBOTE_ENTITY_ID]
    assert [i["summary"] for i in angebote] == ["Frischkaese"]
    assert angebote[0]["description"] == (
        "- Frischkaese Natur - 1.11 € bei Markt B, gueltig bis 2026-10-24\n"
        "- Frischkaese Natur - 1.11 € bei Markt A, gueltig bis 2026-10-24\n"
        "- Frischkaese Natur - 0.99 € bei Markt C, gueltig ab 2026-10-26 bis 2026-10-31"
    )
    # The future offer is still pushed, now with its start date.
    assert ha.pushes == ["3 neue(s) Angebot(e) gefunden, davon 1 ab 2026-10-26 - siehe Einkaufsliste."]


def test_second_run_neither_pushes_nor_rewrites(env):
    ha = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.offers["Frischkaese"] = [make_offer(title="Frischkaese Natur")]
    main.run()
    ha.description_updates.clear()
    ha.pushes.clear()

    main.run()

    assert ha.description_updates == []
    assert ha.pushes == []


def test_failed_fetch_keeps_still_valid_offers(env):
    ha = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.offers["Frischkaese"] = [make_offer(title="Frischkaese Natur")]
    main.run()
    env.offers.clear()

    main.run()

    assert ha.watchlist_description("w1") == "- 1.11 € bei Markt A, gueltig bis 2026-10-24"


def test_new_offer_extends_angebote_list_written_without_list_markers(env):
    ha = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    ha.lists[main.ANGEBOTE_ENTITY_ID].append(
        {"uid": "a1", "summary": "Frischkaese", "description": "Frischkaese Alt - 1.29 € bei Markt Z, gueltig bis 2026-10-24"}
    )
    env.offers["Frischkaese"] = [make_offer(title="Frischkaese Natur")]

    main.run()

    assert ha.lists[main.ANGEBOTE_ENTITY_ID][0]["description"] == (
        "- Frischkaese Alt - 1.29 € bei Markt Z, gueltig bis 2026-10-24\n"
        "- Frischkaese Natur - 1.11 € bei Markt A, gueltig bis 2026-10-24"
    )
