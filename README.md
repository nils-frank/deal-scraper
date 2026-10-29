# deal-scraper

Checks a Home Assistant shopping watchlist against current
[Marktguru](https://www.marktguru.de/) supermarket flyer offers, and pushes a
notification (plus updates a to-do list) when something on the list is on sale. It
also answers ad-hoc questions such as "where can I get butter right now?" from the
same offer store (`deals find`).

The watchlist run completes once per invocation - meant to be triggered by a systemd
timer or cron, twice a week. Next to it, the same image runs a small always-on deal
service (`deals serve`): a JSON API for Home Assistant and an MCP server for Claude
Code and Claude Desktop, answering from the same store.

This is a private, single-user homelab tool with a LAN-only service, shared here as a
code reference. It is not a running public service and not offered for anyone else's
use.

## How it works

1. **Fetch** - reads the watchlist from a Home Assistant `todo.*` entity, then queries
   Marktguru's offer search for each item (`src/deals/marktguru.py`, `src/deals/ha.py`).
   Every fetched offer is stored; searches are cached for 12 hours and capped at 30
   per day across every caller.
2. **Match** - Marktguru's product category rules out offers from other categories,
   then deterministic fuzzy matching (`rapidfuzz`) between watchlist text and offer
   titles, with confident-match/confident-reject score thresholds
   (`src/deals/match.py`). Every verdict is logged.
3. **Escalate the uncertain middle ground** - offers that fuzzy matching can't
   confidently call either way get one inference from a small local LLM
   ([Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF),
   bundled into the image and run via `llama.cpp` as a subprocess) instead of a
   standing model service (`src/deals/llm_escalate.py`). See `DESIGN.md` for why 0.5B wasn't
   good enough and 1.5B was.
4. **Dedup** - a SQLite table tracks what's already been notified about, so the same
   offer doesn't get re-pushed on every run until it expires (`src/deals/store.py`).
5. **Notify** - a Home Assistant push notification for new matches, plus the matched
   offers get written into a to-do list for the shopping trip
   (`src/deals/watchlist_run.py`).

## A note on Marktguru's API

Marktguru's search endpoint isn't a documented public API - it's the same one their
own web frontend calls, with an `apiKey`/`clientKey` pair extracted at runtime from a
script block on their homepage (`src/deals/marktguru.py`). This is how a prior open
source project ([openclaw-supermarket-deals](https://github.com/benmillerat/openclaw-supermarket-deals))
does it too. This runs privately for one household at a handful of requests a week,
with a hard daily cap, isn't affiliated with Marktguru, and isn't run as or offered as
a service to anyone else. Marktguru's terms prohibit automated data extraction - check them
yourself before running this against their service, and don't scale it up.

## Running it

```bash
cp .env.example .env   # fill in real values
docker buildx build --platform linux/arm64 -t deal-scraper:local --load .
docker run --rm --env-file .env -v $(pwd)/data:/data deal-scraper:local
```

Without arguments the image runs the watchlist (`deals run-watchlist`). To ask about
one product instead:

```bash
docker run --rm --env-file .env -v $(pwd)/data:/data deal-scraper:local find Butter --when now
```

`--when` is `now` (valid today, or Monday when asked on a Sunday), `week` or `upcoming`; `--json`
prints the structured result and `--speech` the short German answer meant for Home
Assistant.

See `deploy/` for an example systemd service + timer pair to run it twice weekly, and
`.env.example` for every configuration value.

## The deal service

`deals serve [--host 0.0.0.0] [--port 8000]` runs one long-lived process with one port:

| Path | What it answers |
|---|---|
| `GET /api/find?q=Butter&when=now` | The `find_offers` answer as JSON, plus `speech`, the short German sentence for Home Assistant Assist. Optional `stores=REWE,Lidl` and `limit=` |
| `GET /api/watchlist?when=week` | The `watchlist_deals` answer, for a Home Assistant REST sensor |
| `GET /api/status` | The `deals://status` answer |
| `/mcp` | The MCP server, Streamable HTTP (stateless, JSON responses) |
| `GET /healthz` | Whether the store answers, and the age of the last watchlist run. Open, no token |

Every path except `/healthz` needs `Authorization: Bearer <DEAL_SERVICE_TOKEN>` and
answers 401 without it. The service speaks plain HTTP, so keep it on the LAN; the token
is the access control. It needs `DEAL_SERVICE_TOKEN` (at least 32 characters, e.g.
`openssl rand -hex 32`), `DEAL_SCRAPER_PLZ` and `HA_TOKEN`, and refuses to start without
them. `DB_PATH` must point at the same file the watchlist run uses, so both share one
Marktguru cache and one daily search budget; `SHOPPING_LIST_ENTITY_ID` names the
shopping list (default `todo.mealie_lebensmittel`). See `.env.example`.

```bash
docker run -d --name deal-service --env-file .env -v $(pwd)/data:/data -p 8000:8000 deal-scraper:local serve
curl http://localhost:8000/healthz
curl -H "Authorization: Bearer $DEAL_SERVICE_TOKEN" "http://localhost:8000/api/find?q=Butter"
```

### MCP tools

| Tool | What it does | Marktguru traffic |
|---|---|---|
| `find_offers(query, when="now", stores=None, limit=20)` | Current and upcoming offers grouped by product, cheapest first. Per offer: store, price, unit price, old price, `app_only`, `valid_from`, `valid_to`, a validity label relative to today (`validity` "bis Sa 24.10.", `validity_en` "until Sat 24 Oct"), image URL, link and `offer_key`; plus `fetched_at`, `from_cache` and `budget_left` | At most two searches, cached 12 h |
| `watchlist_deals(when="now")` | What the last watchlist run matched, per watchlist item | None, read from the store |
| `watchlist_add(item)`, `watchlist_remove(item)` | Edit the watchlist to-do list (`TODO_ENTITY_ID`) | None |
| `add_to_shopping_list(offer_key)` | Put an offer on the shopping list, e.g. "Meggle Butter 1,29 € (REWE, bis Sa 31.10.)" | None |

`find_offers` and `watchlist_deals` carry `readOnlyHint`. The three write tools do not,
so Claude asks before each call. The server also offers the resource `deals://status`
(the last watchlist run, live searches used today, Marktguru's health), the prompt
`wochenangebote`, and instructions that tell the agent how to read the offers: flyer
offers for the configured postcode, "where" means a chain, app-only prices are flagged,
every offer is stated with its validity span and coming-up offers are listed separately.

### Claude Code

Keep the token in the macOS login Keychain, not in Claude's configuration:

```bash
security add-generic-password -s deal-scraper -a mcp -w   # prompts for the token
```

A `headersHelper` script hands it to Claude Code on every connection, for example
`~/.local/bin/deals-mcp-headers` (mode 700):

```sh
#!/bin/sh
TOKEN=$(/usr/bin/security find-generic-password -s deal-scraper -a mcp -w) || exit 1
printf '{"Authorization": "Bearer %s"}\n' "$TOKEN"
```

`claude mcp add --transport http deals <url> --header ...` would write the token into
`~/.claude.json`, so register the HTTP server with the helper through `add-json` instead:

```bash
claude mcp add-json -s user deals '{
  "type": "http",
  "url": "http://<deal-host>:<port>/mcp",
  "headersHelper": "/Users/<you>/.local/bin/deals-mcp-headers"
}'
```

### Claude Desktop

Claude Desktop starts local stdio servers, so a launcher bridges to the HTTP endpoint
with a pinned [`mcp-remote`](https://www.npmjs.com/package/mcp-remote), reading the
token from the same Keychain item. For example `~/.local/bin/deals-mcp-launch` (mode
700):

```sh
#!/bin/sh
MCP_REMOTE_VERSION=<pinned version>
AUTH_HEADER="Bearer $(/usr/bin/security find-generic-password -s deal-scraper -a mcp -w)" || exit 1
PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
exec npx -y "mcp-remote@$MCP_REMOTE_VERSION" "http://<deal-host>:<port>/mcp" \
  --transport http-only --header "Authorization:$AUTH_HEADER" --allow-http
```

`--allow-http` lets `mcp-remote` use plain HTTP to a host other than localhost. Then
point `~/Library/Application Support/Claude/claude_desktop_config.json` at the launcher:

```json
{ "mcpServers": { "deals": { "command": "/Users/<you>/.local/bin/deals-mcp-launch" } } }
```

Either client then answers "Where can I get butter right now?" with `find_offers`.

## Tests

Tests use synthetic offers only and need no network access:

```bash
pip install -e ".[dev]"
pytest
```

## Stack

Plain `requests` against Marktguru's search API and Home Assistant's REST API,
`rapidfuzz` for matching, SQLite for the offer store and dedup state, `llama.cpp` + a bundled quantized
model for the LLM escalation tier, and the official MCP Python SDK on Starlette and uvicorn for the
deal service. One arm64-compatible image runs both the batch job and the service (originally
deployed on a Raspberry Pi).

## License

MIT, see `LICENSE`.
