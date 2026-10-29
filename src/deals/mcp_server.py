"""MCP server for Claude Code and Claude Desktop, served at /mcp by `api.py`.

Streamable HTTP, stateless and with JSON responses, so Claude Code connects to
it directly (`--transport http`) and Claude Desktop through mcp-remote. Every
tool is a thin wrapper over DealService. The read tools carry readOnlyHint;
the write tools carry none, so the client asks before each call.
"""
from __future__ import annotations

import json
from importlib.metadata import version
from typing import Annotated, Any, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from deals.query import DEFAULT_LIMIT
from deals.service import MAX_LIMIT, DealService, ServiceError

INSTRUCTIONS = """\
Grocery flyer offers (Prospekt-Angebote) from Marktguru for one household near postcode {plz}.

- Offers are flyer offers for that postcode. "Where" means which chain (REWE, Lidl, ...), never a branch
  or an address: the data has no branches.
- State the validity span of every offer you mention, with weekday and date, e.g. "0.99 € at REWE until
  Sat 24 Oct". Each offer carries it ready-made relative to `today`: `validity_en` in English, `validity`
  in German. List offers that have not started yet (`upcoming`) separately, as coming up.
- Flag app-only prices (`app_only: true`): they need the chain's app or loyalty card.
- Judge whether an offer is the product asked for from its category plus its title: Butterkäse and
  Butterkekse are not butter, and margarine sits in the category "Butter" without being butter. Leave
  out what does not fit and say what you left out.
- find_offers searches Marktguru live; results are cached for 12 hours and live searches are capped per
  day for the whole household (`budget_left`). Do not search again for spelling variants. When
  `from_cache` is true, say how old the prices are (`fetched_at`).
- watchlist_deals reads what the last watchlist run matched and needs no search.
- To put an offer on the shopping list, pass its `offer_key` to add_to_shopping_list. watchlist_add and
  watchlist_remove change the watched products; the next watchlist run searches for them.
"""

WOCHENANGEBOTE = """\
Welche Produkte von meiner Watchlist sind diese Woche im Angebot? Ruf watchlist_deals mit when="week" auf
und fasse das Ergebnis auf Deutsch zusammen: pro Produkt der günstigste Preis mit Kette und Gültigkeit
(Wochentag und Datum), App-Preise gekennzeichnet. Angebote, die erst später beginnen, kommen in einen
eigenen Abschnitt "Demnächst". Nenne zum Schluss die Watchlist-Produkte ohne Angebot."""

READ_ONLY = ToolAnnotations(read_only_hint=True)


def _call(method, *args, **kwargs) -> dict[str, Any]:
    try:
        return method(*args, **kwargs)
    except ServiceError as exc:
        raise ToolError(str(exc)) from exc


def build_server(service: DealService) -> MCPServer:
    server = MCPServer(
        "deals",
        title="Grocery deals",
        version=version("deals"),
        instructions=INSTRUCTIONS.format(plz=service.settings.plz),
    )

    @server.tool(title="Find offers", annotations=READ_ONLY.model_copy(update={"open_world_hint": True}))
    def find_offers(
        query: Annotated[str, Field(description='The product to look for, e.g. "Butter" or "Philadelphia"')],
        when: Annotated[
            Literal["now", "week", "upcoming"],
            Field(description="now: valid today (on a Sunday: Monday), plus what starts within 7 days as upcoming; "
                              "week: valid at some point in the next 7 days; upcoming: starts later"),
        ] = "now",
        stores: Annotated[
            Optional[list[str]], Field(description='Only these chains, e.g. ["REWE", "Lidl"]; default all')
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description="At most this many products per list")] = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Where a product is on offer: current and upcoming flyer offers grouped by product, cheapest first.
        Per offer: store (the chain), price, unit_price per unit, old_price, app_only, valid_from, valid_to,
        validity and validity_en labels, image_url, source_url and offer_key. Also fetched_at, from_cache,
        budget_left, the categories left out, and a short German answer (speech)."""
        return _call(service.find_offers, query, when, stores, limit)

    @server.tool(title="Watchlist deals", annotations=READ_ONLY)
    def watchlist_deals(
        when: Annotated[
            Literal["now", "week", "upcoming"], Field(description="now, week or upcoming, as for find_offers")
        ] = "now",
    ) -> dict[str, Any]:
        """What is on offer from the household's watchlist, as matched by the last watchlist run, grouped per
        watchlist item into current and upcoming offers. Needs no Marktguru search."""
        return _call(service.watchlist_deals, when)

    @server.tool(title="Add to watchlist", annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    def watchlist_add(
        item: Annotated[str, Field(description='The product to watch, as the household would write it, e.g. "Butter"')],
    ) -> dict[str, Any]:
        """Adds a product to the watchlist to-do list in Home Assistant. The next watchlist run searches
        offers for it and pushes a notification when one turns up."""
        return _call(service.watchlist_add, item)

    @server.tool(title="Remove from watchlist", annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    def watchlist_remove(
        item: Annotated[str, Field(description="The watchlist entry to remove; case does not matter")],
    ) -> dict[str, Any]:
        """Removes a product from the watchlist to-do list in Home Assistant."""
        return _call(service.watchlist_remove, item)

    @server.tool(title="Add to shopping list", annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    def add_to_shopping_list(
        offer_key: Annotated[str, Field(description='The offer_key of an offer from find_offers or watchlist_deals, e.g. "marktguru:123"')],
    ) -> dict[str, Any]:
        """Puts an offer on the household's shopping list in Home Assistant, with its price, chain and
        validity, e.g. "Meggle Butter 1,29 € (REWE, bis Sa 31.10.)"."""
        return _call(service.add_to_shopping_list, offer_key)

    @server.resource(
        "deals://status",
        name="status",
        title="Deal service status",
        description="The last watchlist run, the live searches used today and Marktguru's health",
        mime_type="application/json",
    )
    def status() -> str:
        return json.dumps(service.status(), ensure_ascii=False, indent=2)

    @server.prompt(name="wochenangebote", title="Wochenangebote", description="Die Angebote dieser Woche für die Watchlist")
    def wochenangebote() -> str:
        return WOCHENANGEBOTE

    return server
