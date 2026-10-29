# deal-scraper

Checks a Home Assistant shopping watchlist against current
[Marktguru](https://www.marktguru.de/) supermarket flyer offers, and pushes a
notification (plus updates a to-do list) when something on the list is on sale. It
also answers ad-hoc questions such as "where can I get butter right now?" from the
same offer store (`deals find`).

The watchlist run completes once per invocation - meant to be triggered by a systemd
timer or cron, twice a week. `DESIGN.md` describes the planned deal service built on
the same core.

This is a private, single-user homelab tool shared here as a code reference, not a
running public service and not offered for anyone else's use.

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

Tests use synthetic offers only and need no network access:

```bash
pip install -e ".[dev]"
pytest
```

## Stack

Plain `requests` against Marktguru's search API and Home Assistant's REST API,
`rapidfuzz` for matching, SQLite for the offer store and dedup state, `llama.cpp` + a bundled quantized
model for the LLM escalation tier. Single-container batch job, arm64-compatible
(originally deployed on a Raspberry Pi).

## License

MIT, see `LICENSE`.
