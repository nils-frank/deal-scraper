"""watchlist_run.run() end to end, with Home Assistant and Marktguru's HTTP
endpoints replaced by fakes; the store, cache, budget and matcher are real."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
import requests

from conftest import NOW, FakeMarktguru, make_offer, raw_offer
from deals import ha, marktguru, store, watchlist_run


class FakeHomeAssistant:
    def __init__(self, watchlist: list[dict]):
        self.lists = {watchlist_run.TODO_ENTITY_ID: watchlist, watchlist_run.ANGEBOTE_ENTITY_ID: []}
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
        return next(i["description"] for i in self.lists[watchlist_run.TODO_ENTITY_ID] if i["uid"] == uid)

    def watchlist_updates(self):
        return [u for u in self.description_updates if u[0] == watchlist_run.TODO_ENTITY_ID]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Returns a function that installs a fake HA with the given watchlist.
    env.marktguru serves synthetic raw offers per query; env.escalations records
    LLM calls and env.llm_says decides them."""
    db_path = str(tmp_path / "run.db")
    monkeypatch.setenv("DB_PATH", db_path)
    clock = {"now": NOW}
    monkeypatch.setattr(watchlist_run, "utcnow", lambda: clock["now"])
    fake = FakeMarktguru()
    monkeypatch.setattr(marktguru, "new_session", lambda: fake)

    def escalate(summary, title):
        install.escalations.append((summary, title))
        return install.llm_says

    monkeypatch.setattr(watchlist_run, "escalate", escalate)

    def install(watchlist):
        ha_fake = FakeHomeAssistant(watchlist)
        for name in ("get_todo_items", "update_todo_item_description", "add_todo_item", "remove_todo_item", "notify"):
            monkeypatch.setattr(ha, name, getattr(ha_fake, name))
        return ha_fake

    def advance(**delta):
        clock["now"] += timedelta(**delta)

    install.marktguru = fake
    install.db_path = db_path
    install.escalations = []
    install.llm_says = False
    install.advance = advance
    return install


def frischkaese(offer_id, retailer, price, **fields):
    return raw_offer(offer_id, product="Frischkaese Natur", retailer=retailer, price=price, **fields)


def test_expired_offer_is_cleared_from_watchlist(env):
    expired = make_offer(title="Tomaten - Passiert", retailer="Markt K", price=0.99,
                         valid_from=date(2026, 10, 12), valid_to=date(2026, 10, 17))
    with store.open_store(env.db_path) as conn:
        store.record_notified(conn, expired, "Tomaten", "Tomaten")
    ha_fake = env([{"uid": "w1", "summary": "Tomaten", "description": "0.99 € bei Markt K, gueltig bis 2026-10-17"}])
    ha_fake.lists[watchlist_run.ANGEBOTE_ENTITY_ID].append({"uid": "a1", "summary": "Tomaten", "description": "old line"})

    assert watchlist_run.run() == 0

    assert ha_fake.watchlist_description("w1") is None
    assert ha_fake.watchlist_updates() == [(watchlist_run.TODO_ENTITY_ID, "w1", None)]
    assert ha_fake.lists[watchlist_run.ANGEBOTE_ENTITY_ID] == []
    assert ha_fake.pushes == []


def test_all_stores_listed_current_first_and_future_offer_marked(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.marktguru.results["Frischkaese"] = [
        frischkaese(1, "Markt B", 1.11),
        frischkaese(2, "Markt A", 1.11),
        frischkaese(3, "Markt C", 0.99, valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31)),
        raw_offer(4, product="Fahrradhelm Sport", retailer="Markt D", price=19.99, category="Sport"),
    ]

    watchlist_run.run()

    assert ha_fake.watchlist_description("w1") == (
        "- 1.11 € bei Markt A, gueltig bis 2026-10-24\n"
        "- 1.11 € bei Markt B, gueltig bis 2026-10-24\n"
        "- 0.99 € bei Markt C, gueltig ab 2026-10-26 bis 2026-10-31"
    )
    angebote = ha_fake.lists[watchlist_run.ANGEBOTE_ENTITY_ID]
    assert [i["summary"] for i in angebote] == ["Frischkaese"]
    assert angebote[0]["description"] == (
        "- Frischkaese Natur - 1.11 € bei Markt B, gueltig bis 2026-10-24\n"
        "- Frischkaese Natur - 1.11 € bei Markt A, gueltig bis 2026-10-24\n"
        "- Frischkaese Natur - 0.99 € bei Markt C, gueltig ab 2026-10-26 bis 2026-10-31"
    )
    # The future offer is still pushed, now with its start date.
    assert ha_fake.pushes == ["3 neue(s) Angebot(e) gefunden, davon 1 ab 2026-10-26 - siehe Einkaufsliste."]


def test_every_fetched_offer_and_verdict_is_stored(env):
    env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.marktguru.results["Frischkaese"] = [
        frischkaese(1, "Markt A", 1.11),
        raw_offer(2, product="Frischkaesezubereitung", retailer="Markt B", category=None),
        raw_offer(3, product="Frischkaese Torte", retailer="Markt C", category="Kuchen"),
        raw_offer(4, product="Fahrradhelm Sport", retailer="Markt D", category=None),
    ]

    watchlist_run.run()

    with store.open_store(env.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 4
        assert conn.execute("SELECT query_norm, ok FROM fetches").fetchall() == [("frischkaese", 1)]
        verdicts = conn.execute("SELECT offer_key, decision, method FROM matches ORDER BY offer_key").fetchall()
    assert verdicts == [
        ("marktguru:1", "match", "fuzzy"),
        ("marktguru:2", "no_match", "llm"),
        ("marktguru:3", "no_match", "category"),
        ("marktguru:4", "no_match", "fuzzy"),
    ]
    # Only the uncertain middle reached the local model; the category kept the cake away.
    assert env.escalations == [("Frischkaese", "Frischkaesezubereitung")]


def test_finished_run_is_recorded(env):
    env([{"uid": "w1", "summary": "Frischkaese", "description": None}, {"uid": "w2", "summary": "Tomaten", "description": None}])
    env.marktguru.results["Frischkaese"] = [frischkaese(1, "Markt A", 1.11)]

    watchlist_run.run()

    with store.open_store(env.db_path) as conn:
        assert store.last_watchlist_run(conn) == {"finished_at": NOW, "items": ["Frischkaese", "Tomaten"], "new_matches": 1}


def test_empty_watchlist_run_is_recorded(env):
    env([])
    assert watchlist_run.run() == 0
    with store.open_store(env.db_path) as conn:
        assert store.last_watchlist_run(conn) == {"finished_at": NOW, "items": [], "new_matches": 0}


def test_llm_yes_is_pushed(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.marktguru.results["Frischkaese"] = [raw_offer(2, product="Frischkaesezubereitung", category=None)]
    env.llm_says = True

    watchlist_run.run()

    assert ha_fake.pushes == ["1 neue(s) Angebot(e) gefunden - siehe Einkaufsliste."]


def test_second_run_neither_pushes_nor_rewrites(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.marktguru.results["Frischkaese"] = [frischkaese(1, "Markt A", 1.11)]
    watchlist_run.run()
    ha_fake.description_updates.clear()
    ha_fake.pushes.clear()
    env.advance(days=1)

    watchlist_run.run()

    assert env.marktguru.searches == ["Frischkaese", "Frischkaese"]
    assert ha_fake.description_updates == []
    assert ha_fake.pushes == []


def test_failed_fetch_keeps_still_valid_offers(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.marktguru.results["Frischkaese"] = [frischkaese(1, "Markt A", 1.11)]
    watchlist_run.run()
    env.advance(days=1)
    env.marktguru.fail = requests.ConnectionError("down")
    ha_fake.pushes.clear()

    assert watchlist_run.run() == 0

    assert ha_fake.watchlist_description("w1") == "- 1.11 € bei Markt A, gueltig bis 2026-10-24"
    assert ha_fake.pushes == []


def test_ended_offer_from_stored_fetch_is_not_pushed_again(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.marktguru.results["Frischkaese"] = [frischkaese(1, "Markt A", 1.11)]
    watchlist_run.run()
    env.advance(days=4)
    env.marktguru.fail = requests.ConnectionError("down")
    ha_fake.pushes.clear()

    assert watchlist_run.run() == 0

    assert ha_fake.lists[watchlist_run.ANGEBOTE_ENTITY_ID] == []
    assert ha_fake.watchlist_description("w1") is None
    assert ha_fake.pushes == []
    assert env.escalations == []


def test_ended_offer_is_not_pushed_again_once_the_budget_is_spent(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.marktguru.results["Frischkaese"] = [frischkaese(1, "Markt A", 1.11)]
    watchlist_run.run()
    env.advance(days=4)
    monday = NOW + timedelta(days=4)
    with store.open_store(env.db_path) as conn:
        for i in range(marktguru.DAILY_BUDGET):
            store.reserve_search(conn, f"frage {i}", "00000", monday, monday - timedelta(hours=1), marktguru.DAILY_BUDGET)
    calls_before = len(env.marktguru.calls)
    ha_fake.pushes.clear()

    assert watchlist_run.run() == 0

    assert len(env.marktguru.calls) == calls_before
    assert ha_fake.lists[watchlist_run.ANGEBOTE_ENTITY_ID] == []
    assert ha_fake.watchlist_description("w1") is None
    assert ha_fake.pushes == []


def test_offer_ended_overnight_is_not_pushed_again_from_the_cache(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    env.marktguru.results["Frischkaese"] = [frischkaese(1, "Markt A", 1.11, valid_to=date(2026, 10, 22))]
    env.advance(hours=13)  # 22:00 on the offer's last day
    watchlist_run.run()
    assert len(ha_fake.pushes) == 1
    env.advance(hours=10)  # 08:00 the next morning, the search is still cached
    ha_fake.pushes.clear()

    assert watchlist_run.run() == 0

    assert env.marktguru.searches == ["Frischkaese"]
    assert ha_fake.lists[watchlist_run.ANGEBOTE_ENTITY_ID] == []
    assert ha_fake.pushes == []


def test_exhausted_budget_makes_no_request(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    with store.open_store(env.db_path) as conn:
        for i in range(marktguru.DAILY_BUDGET):
            store.reserve_search(conn, f"frage {i}", "00000", NOW, NOW - timedelta(hours=1), marktguru.DAILY_BUDGET)

    assert watchlist_run.run() == 0

    assert env.marktguru.calls == []
    assert ha_fake.pushes == []


def test_new_offer_extends_angebote_list_written_without_list_markers(env):
    ha_fake = env([{"uid": "w1", "summary": "Frischkaese", "description": None}])
    ha_fake.lists[watchlist_run.ANGEBOTE_ENTITY_ID].append(
        {"uid": "a1", "summary": "Frischkaese", "description": "Frischkaese Alt - 1.29 € bei Markt Z, gueltig bis 2026-10-24"}
    )
    env.marktguru.results["Frischkaese"] = [frischkaese(1, "Markt A", 1.11)]

    watchlist_run.run()

    assert ha_fake.lists[watchlist_run.ANGEBOTE_ENTITY_ID][0]["description"] == (
        "- Frischkaese Alt - 1.29 € bei Markt Z, gueltig bis 2026-10-24\n"
        "- Frischkaese Natur - 1.11 € bei Markt A, gueltig bis 2026-10-24"
    )
