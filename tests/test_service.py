"""The deal service end to end: a real uvicorn server on a local port, called
through its JSON API with requests and through its MCP endpoint with the MCP
SDK's own Streamable HTTP client. Marktguru and Home Assistant are fakes at
the HTTP level; the store, cache, budget, query engine and `ha` client are real."""
from __future__ import annotations

import json
import socket
import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace

import anyio
import httpx2
import pytest
import requests
import uvicorn
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.client.streamable_http import streamable_http_client

from conftest import NOW, FakeResponse, raw_offer
from deals import api, cli, ha, marktguru, service, store
from deals.service import ConfigError, DealService, Settings

TOKEN = "s3cret-" * 6
WATCHLIST = "todo.watchlist"
SHOPPING = "todo.shopping"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Thursday NOW: three Butter offers running until Saturday or starting Monday,
# one of them an app price, plus Butterkaese in another category.
BUTTER = [
    raw_offer(1, brand="Almhof", product="Butter", retailer="Markt A", price=0.99, unit_price=3.96, volume=0.25,
              category="Butter", description="je 250 g"),
    raw_offer(2, brand="Bergwiese", product="Feine Butter", retailer="Markt C", price=1.29, unit_price=5.16, volume=0.25,
              category="Butter", description="HINWEIS: MIT APP 0,10 € BONUS, je 250 g", oldPrice=1.79),
    raw_offer(3, brand="Weidegold", product="Beste Butter", retailer="Markt F", price=1.49, unit_price=5.96, volume=0.25,
              category="Butter", valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31)),
    raw_offer(4, brand="Almhof", product="Butterkaese", retailer="Markt B", price=0.89, category="Käse"),
]
ENDED = raw_offer(5, brand="Almhof", product="Butter", retailer="Markt D", price=0.79, category="Butter",
                  valid_from=date(2026, 10, 12), valid_to=date(2026, 10, 17))


class FakeHomeAssistant:
    """Home Assistant's todo services at the requests.post level, so the real `ha` client runs."""

    def __init__(self):
        self.lists = {
            WATCHLIST: [{"uid": "w1", "summary": "Butter", "status": "needs_action"}],
            SHOPPING: [{"uid": "s1", "summary": "Milch", "status": "needs_action"}],
        }
        self.writes: list[tuple[str, dict]] = []
        self.status = 200

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        assert headers["Authorization"] == "Bearer test-token"
        name = url.split("/api/services/", 1)[1]
        if self.status != 200:
            return FakeResponse(self.status, "")
        items = self.lists[json["entity_id"]]
        if name == "todo/get_items":
            assert params == {"return_response": "true"}
            return FakeResponse(200, _dumps({"service_response": {json["entity_id"]: {"items": items}}}))
        self.writes.append((name, json))
        if name == "todo/add_item":
            items.append({"uid": f"new{len(self.writes)}", "summary": json["item"], "status": "needs_action"})
        elif name == "todo/remove_item":
            items[:] = [i for i in items if json["item"] not in (i["uid"], i["summary"])]
        return FakeResponse(200, "[]")

    def summaries(self, entity):
        return [i["summary"] for i in self.lists[entity]]


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def fake_ha(monkeypatch):
    fake = FakeHomeAssistant()
    monkeypatch.setattr(ha, "requests", SimpleNamespace(post=fake.post))
    return fake


@pytest.fixture
def deals(tmp_path, monkeypatch, fake_marktguru, clock, fake_ha):
    fake_marktguru.results["Butter"] = BUTTER + [ENDED]
    monkeypatch.setattr(marktguru, "new_session", lambda: fake_marktguru)
    settings = Settings(plz="00000", token=TOKEN, db_path=str(tmp_path / "service.db"),
                        watchlist_entity=WATCHLIST, shopping_list_entity=SHOPPING)
    deal_service = DealService(settings, clock=clock)
    deal_service.migrate()
    return deal_service


@pytest.fixture
def server(deals):
    """The app under uvicorn on a free local port; yields its base URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    uvicorn_server = uvicorn.Server(uvicorn.Config(api.create_app(deals), log_level="warning"))
    thread = threading.Thread(target=uvicorn_server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uvicorn_server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    uvicorn_server.should_exit = True
    thread.join(10)


def get(server, path, headers=AUTH, **params):
    return requests.get(f"{server}{path}", params=params, headers=headers, timeout=10)


def mcp(server, mode="auto", token=TOKEN):
    """Runs `steps(client)` against the server's /mcp in a fresh MCP session."""
    def run(steps):
        async def main():
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
                async with Client(streamable_http_client(f"{server}/mcp", http_client=http), mode=mode) as client:
                    return await steps(client)

        return anyio.run(main)

    return run


def offers(result, part="current"):
    return [offer for group in result[part] for offer in group["offers"]]


# --- auth ---


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
@pytest.mark.parametrize("path", ["/api/find", "/api/watchlist", "/api/status", "/mcp", "/elsewhere"])
def test_requests_without_the_token_get_401(server, fake_marktguru, path, headers):
    response = get(server, path, headers=headers, q="Butter")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert fake_marktguru.calls == []


def test_mcp_post_without_the_token_gets_401(server):
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert requests.post(f"{server}/mcp", json=initialize, timeout=10).status_code == 401


def test_healthz_is_open_and_reports_the_last_watchlist_run(server, deals, clock):
    response = get(server, "/healthz", headers={})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "store": "ok", "last_watchlist_run": None,
                               "last_watchlist_run_age_seconds": None}

    with store.open_store(deals.settings.db_path) as conn:
        store.record_watchlist_run(conn, NOW, ["Butter"], 2)
    clock.advance(hours=3)
    body = get(server, "/healthz", headers={}).json()
    assert body["last_watchlist_run"] == "2026-10-22T07:00:00+00:00"
    assert body["last_watchlist_run_age_seconds"] == 3 * 3600


def test_healthz_reports_an_unreachable_store(server, deals, tmp_path):
    deals.settings = Settings(plz="00000", token=TOKEN, db_path=str(tmp_path / "missing" / "service.db"))
    response = get(server, "/healthz", headers={})
    assert response.status_code == 503
    assert response.json() == {"status": "error", "store": "unreachable"}


def test_a_lan_host_name_is_accepted(server):
    """Claude reaches the service by its LAN address, not localhost."""
    response = requests.post(
        f"{server}/mcp",
        headers={**AUTH, "Host": "deals.lan:30083", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        timeout=10,
    )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "deals"


# --- JSON API ---


def test_api_find_answers_with_validity_and_speech(server, fake_marktguru):
    result = get(server, "/api/find", q="Butter").json()
    assert fake_marktguru.searches == ["Butter"]
    assert [g["name"] for g in result["current"]] == ["Almhof Butter", "Bergwiese Feine Butter"]
    assert result["excluded_categories"] == {"Käse": 1}
    almhof, bergwiese = offers(result)
    expected = {
        "offer_key": "marktguru:1", "store": "Markt A", "category": "Butter", "price": 0.99, "unit_price": 3.96, "unit": "kg",
        "old_price": None, "pack_size": "250 g", "app_only": False, "valid_from": "2026-10-19", "valid_to": "2026-10-24",
        "validity": "bis Sa 24.10.", "validity_en": "until Sat 24 Oct",
        "image_url": "https://cdn.marktguru.de/api/v1/offers/1/images/default/0/small.webp",
        "source_url": "https://www.marktguru.de/offers/1",
    }
    assert {key: almhof[key] for key in expected} == expected
    assert (bergwiese["app_only"], bergwiese["old_price"]) == (True, 1.79)
    [weidegold] = offers(result, "upcoming")
    assert (weidegold["validity"], weidegold["validity_en"]) == ("ab Mo 26.10. bis Sa 31.10.", "from Mon 26 Oct until Sat 31 Oct")
    assert result["fetched_at"] == NOW.isoformat()
    assert (result["from_cache"], result["budget_left"]) == (False, marktguru.DAILY_BUDGET - 1)
    assert result["speech"] == (
        "Butter im Angebot: Almhof Butter 0,99 € bei Markt A, Bergwiese Feine Butter 1,29 € bei Markt C, alle bis Samstag. "
        "Demnächst: ab Montag bis 31. Oktober Weidegold Beste Butter 1,49 € bei Markt F."
    )


def test_api_find_filters_stores_and_limits(server):
    result = get(server, "/api/find", q="Butter", when="week", stores="Markt C,Markt F", limit=1).json()
    assert [o["store"] for o in offers(result)] == ["Markt C"]
    assert [o["store"] for o in offers(result, "upcoming")] == ["Markt F"]


@pytest.mark.parametrize(("params", "error"), [
    ({"q": " "}, "the query must not be empty"),
    ({"q": "Butter", "when": "today"}, "when must be one of now, week, upcoming, not 'today'"),
    ({"q": "Butter", "limit": "many"}, "limit must be a number"),
    ({"q": "Butter", "limit": "0"}, "limit must be between 1 and 50"),
])
def test_api_find_rejects_bad_parameters(server, fake_marktguru, params, error):
    response = get(server, "/api/find", **params)
    assert (response.status_code, response.json()) == (400, {"error": error})
    assert fake_marktguru.calls == []


# --- watchlist_deals ---


def seed_watchlist_run(deals, items=("Butter", "Frischkaese")):
    """What a watchlist run leaves behind: the searched offers, the verdicts and the run record."""
    with store.open_store(deals.settings.db_path) as conn:
        client = marktguru.Client(conn, "00000", clock=deals.clock)
        searched = client.search("Butter").offers
        for offer in searched:
            decision = "match" if offer.category == "Butter" else "no_match"
            store.record_match(conn, "Butter", offer.offer_id, 90, decision, "fuzzy", NOW)
        # A later verdict overrides an earlier one.
        store.record_match(conn, "Butter", "marktguru:2", 60, "no_match", "llm", NOW + timedelta(minutes=1))
        store.record_match(conn, "Alte Sache", "marktguru:1", 90, "match", "fuzzy", NOW)
        store.record_watchlist_run(conn, NOW, list(items), 3)


def test_api_watchlist_reads_the_last_run_without_searching(server, deals, fake_marktguru):
    seed_watchlist_run(deals)
    searches = len(fake_marktguru.searches)
    result = get(server, "/api/watchlist", when="now").json()
    assert len(fake_marktguru.searches) == searches
    assert result["today"] == "2026-10-22"
    assert result["without_offers"] == ["Frischkaese"]
    [butter] = result["items"]
    assert butter["item"] == "Butter"
    assert [(o["offer_key"], o["validity"]) for o in offers(butter)] == [("marktguru:1", "bis Sa 24.10.")]
    assert [(o["offer_key"], o["validity_en"]) for o in offers(butter, "upcoming")] == [
        ("marktguru:3", "from Mon 26 Oct until Sat 31 Oct")
    ]
    assert result["last_watchlist_run"] == {"finished_at": NOW.isoformat(), "age_hours": 0.0, "items": 2, "new_matches": 3}


def test_watchlist_deals_upcoming_only(deals):
    seed_watchlist_run(deals)
    result = deals.watchlist_deals("upcoming")
    assert offers(result["items"][0]) == []
    assert [o["offer_key"] for o in offers(result["items"][0], "upcoming")] == ["marktguru:3"]


def test_watchlist_deals_before_the_first_recorded_run(deals):
    with store.open_store(deals.settings.db_path) as conn:
        offer = marktguru.Client(conn, "00000", clock=deals.clock).search("Butter").offers[0]
        store.record_match(conn, "Butter", offer.offer_id, 90, "match", "fuzzy", NOW)
    result = deals.watchlist_deals()
    assert [i["item"] for i in result["items"]] == ["Butter"]
    assert result["last_watchlist_run"] is None


# --- status ---


def test_status_reports_budget_and_marktguru_health(server, deals, fake_marktguru):
    status = get(server, "/api/status").json()
    assert status["budget"] == {"daily": marktguru.DAILY_BUDGET, "used_today": 0, "left": marktguru.DAILY_BUDGET}
    assert status["marktguru"] == {"health": "unknown", "last_search_at": None, "last_success_at": None}
    assert status["last_watchlist_run"] is None

    get(server, "/api/find", q="Butter")
    fake_marktguru.search_status = 503
    deals.clock.advance(minutes=5)
    get(server, "/api/find", q="Quark")
    deals.clock.advance(seconds=marktguru.SEARCH_DEADLINE.total_seconds())
    status = get(server, "/api/status").json()
    assert status["budget"]["used_today"] == 2
    assert status["marktguru"] == {
        "health": "failing",
        "last_search_at": (NOW + timedelta(minutes=5)).isoformat(),
        "last_success_at": NOW.isoformat(),
    }


def test_status_does_not_count_a_search_still_running(deals, fake_marktguru):
    deals.find_offers("Butter")
    deals.clock.advance(minutes=5)
    with store.open_store(deals.settings.db_path) as conn:
        store.reserve_search(conn, "quark", "00000", deals.clock(), NOW, marktguru.DAILY_BUDGET)
    running = deals.status()
    assert running["budget"]["used_today"] == 2
    assert running["marktguru"] == {"health": "ok", "last_search_at": NOW.isoformat(), "last_success_at": NOW.isoformat()}

    deals.clock.advance(seconds=marktguru.SEARCH_DEADLINE.total_seconds())
    assert deals.status()["marktguru"] == {
        "health": "failing",
        "last_search_at": (NOW + timedelta(minutes=5)).isoformat(),
        "last_success_at": NOW.isoformat(),
    }


# --- MCP ---


READ_TOOLS = {"find_offers", "watchlist_deals"}
WRITE_TOOLS = {"watchlist_add", "watchlist_remove", "add_to_shopping_list"}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_mcp_round_trip(server, mode):
    """`legacy` is the initialize handshake mcp-remote and older clients use."""
    async def steps(client):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        status = await client.read_resource("deals://status")
        prompt = await client.get_prompt("wochenangebote")
        found = await client.call_tool("find_offers", {"query": "Butter"})
        return client.instructions, tools, status, prompt, found

    instructions, tools, status, prompt, found = mcp(server, mode)(steps)
    assert "postcode 00000" in instructions
    assert "never a branch" in instructions and "app-only" in instructions and "weekday and date" in instructions
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS:
        assert tools[name].annotations.read_only_hint is True
    for name in WRITE_TOOLS:
        assert tools[name].annotations.read_only_hint is None
    assert set(tools["find_offers"].input_schema["properties"]) == {"query", "when", "stores", "limit"}
    assert json.loads(status.contents[0].text)["budget"]["daily"] == marktguru.DAILY_BUDGET
    assert 'watchlist_deals mit when="week"' in prompt.messages[0].content.text
    assert not found.is_error
    result = found.structured_content
    assert [g["name"] for g in result["current"]] == ["Almhof Butter", "Bergwiese Feine Butter"]
    assert offers(result)[0]["validity_en"] == "until Sat 24 Oct"
    assert json.loads(found.content[0].text) == result


def test_mcp_needs_the_token(server):
    with pytest.raises(ExceptionGroup) as failure:
        mcp(server, token="wrong")(lambda client: client.list_tools())
    assert failure.group_contains(MCPError, match="error response", depth=2)


def test_api_and_mcp_share_cache_and_budget(server, fake_marktguru):
    from_api = get(server, "/api/find", q="Butter").json()

    async def steps(client):
        cached = await client.call_tool("find_offers", {"query": "butter", "when": "week"})
        fresh = await client.call_tool("find_offers", {"query": "Quark"})
        return cached.structured_content, fresh.structured_content

    from_mcp, quark = mcp(server)(steps)
    assert fake_marktguru.searches == ["Butter", "Quark"]
    assert (from_api["from_cache"], from_mcp["from_cache"]) == (False, True)
    assert from_mcp["fetched_at"] == from_api["fetched_at"]
    assert from_mcp["budget_left"] == from_api["budget_left"] == marktguru.DAILY_BUDGET - 1
    assert quark["budget_left"] == marktguru.DAILY_BUDGET - 2
    assert get(server, "/api/status").json()["budget"]["used_today"] == 2


def test_mcp_tool_errors_are_tool_results(server):
    async def steps(client):
        return await client.call_tool("find_offers", {"query": "Butter", "limit": 0})

    result = mcp(server)(steps)
    assert result.is_error


# --- write tools against a fake Home Assistant ---


def test_watchlist_add_and_remove(server, fake_ha):
    async def steps(client):
        added = await client.call_tool("watchlist_add", {"item": "  Philadelphia  Nature "})
        again = await client.call_tool("watchlist_add", {"item": "philadelphia nature"})
        removed = await client.call_tool("watchlist_remove", {"item": "BUTTER"})
        missing = await client.call_tool("watchlist_remove", {"item": "Quark"})
        return added, again, removed, missing

    added, again, removed, missing = mcp(server)(steps)
    assert added.structured_content["added"] is True
    assert again.structured_content == {"added": False, "item": "Philadelphia Nature", "list": WATCHLIST,
                                        "reason": "already on the watchlist"}
    assert removed.structured_content == {"removed": True, "item": "Butter", "list": WATCHLIST}
    assert missing.is_error and "it has Philadelphia Nature" in missing.content[0].text
    assert fake_ha.summaries(WATCHLIST) == ["Philadelphia Nature"]
    assert fake_ha.writes == [
        ("todo/add_item", {"entity_id": WATCHLIST, "item": "Philadelphia Nature"}),
        ("todo/remove_item", {"entity_id": WATCHLIST, "item": "Butter"}),
    ]


def test_add_to_shopping_list(server, deals, fake_ha):
    get(server, "/api/find", q="Butter")

    async def steps(client):
        app_price = await client.call_tool("add_to_shopping_list", {"offer_key": "marktguru:2"})
        twice = await client.call_tool("add_to_shopping_list", {"offer_key": "marktguru:2"})
        upcoming = await client.call_tool("add_to_shopping_list", {"offer_key": "marktguru:3"})
        ended = await client.call_tool("add_to_shopping_list", {"offer_key": "marktguru:5"})
        unknown = await client.call_tool("add_to_shopping_list", {"offer_key": "marktguru:999"})
        return app_price, twice, upcoming, ended, unknown

    app_price, twice, upcoming, ended, unknown = mcp(server)(steps)
    assert app_price.structured_content["item"] == "Bergwiese Feine Butter 1,29 € (Markt C, bis Sa, App-Preis)"
    assert app_price.structured_content["offer"]["validity"] == "bis Sa 24.10."
    assert twice.structured_content["added"] is False
    assert upcoming.structured_content["item"] == "Weidegold Beste Butter 1,49 € (Markt F, ab Mo)"
    assert ended.is_error and "ended on 2026-10-17" in ended.content[0].text
    assert unknown.is_error and "no stored offer 'marktguru:999'" in unknown.content[0].text
    assert fake_ha.summaries(SHOPPING) == [
        "Milch", "Bergwiese Feine Butter 1,29 € (Markt C, bis Sa, App-Preis)", "Weidegold Beste Butter 1,49 € (Markt F, ab Mo)",
    ]


def test_add_to_shopping_list_again_on_a_later_day(server, deals, fake_ha):
    get(server, "/api/find", q="Butter")
    first = deals.add_to_shopping_list("marktguru:3")
    deals.clock.advance(days=4)
    again = deals.add_to_shopping_list("marktguru:3")
    assert first["item"] == "Weidegold Beste Butter 1,49 € (Markt F, ab Mo)"
    assert again["added"] is False
    assert again["offer"]["validity"] == "bis Sa 31.10."
    assert again["item"] == first["item"]
    assert fake_ha.summaries(SHOPPING) == ["Milch", first["item"]]


def test_write_tools_report_home_assistant_failures(deals, fake_ha):
    fake_ha.status = 500
    with pytest.raises(service.UpstreamError, match=r"Home Assistant did not answer \(HTTP 500\)"):
        deals.watchlist_add("Quark")


# --- configuration ---


def test_settings_need_the_token(monkeypatch):
    env = {"DEAL_SCRAPER_PLZ": "00000", "HA_TOKEN": "x", "DEAL_SERVICE_TOKEN": TOKEN}
    assert Settings.from_env(env).token == TOKEN
    with pytest.raises(ConfigError, match="DEAL_SERVICE_TOKEN"):
        Settings.from_env({**env, "DEAL_SERVICE_TOKEN": ""})
    with pytest.raises(ConfigError, match="at least 32 characters"):
        Settings.from_env({**env, "DEAL_SERVICE_TOKEN": "short"})


def test_serve_refuses_to_start_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("DEAL_SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("the service started"))
    assert cli.main(["serve", "--port", "0"]) == 2
    assert "DEAL_SERVICE_TOKEN" in capsys.readouterr().err
