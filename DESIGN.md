# Local Grocery Deal Aggregator — Implementation Plan

**Status:** Implemented and running on `raspi4-02` as a systemd timer
(Mon/Thu 08:00) that starts the image with `docker run --rm`, see `deploy/`.
It moved there on 2026-08-21; before that it ran as a k3s CronJob, which is
what the deployment-related "Decisions" and the "Deployment layout" below were
written for. For day-to-day operation, manual triggering, and troubleshooting,
see the "Deal scraper" section in `OPERATIONS.md` - that is now the maintained
runbook. This document stays as the design record: apart from the deployment
change and the kaufda.de fallback (planned, never built), the "Decisions"
below are what was actually built.

**Notification design** (decided after the "Decisions" below were already
locked, once a plain single push per match proved easy to miss): each run
rewrites every watchlist to-do item's description from its still-valid matched
offers (per store one line for its cheapest offer valid today and one for its
cheapest upcoming offer, current offers first, each group cheapest first,
cleared once all of them have expired), pushes the full match list to a
live-updating dashboard view ("Angebote", `lovelace/deal-scraper`), and sends
**one** consolidated push per run instead of one per match. An offer that starts later shows "ab <date>" in
the descriptions and the push. See `OPERATIONS.md` for the current known issues
with this (tap-to-navigate on the push not yet working).

## Current design: a homelab deal service (architecture B)

Decided on 2026-10-24 ("lets go with B for now"): deal-scraper grows from a
twice-weekly batch job into a small homelab deal service. Current grocery
offers can then be asked for conversationally ("where can I get butter right
now?") through an agent in Claude Code and Claude Desktop, and Home Assistant
can show more than text. It is built in phases:

| Phase | Scope | Status |
|---|---|---|
| 1. Core | `src/deals/` package; offer store v2 with an in-place migration; Marktguru client with key cache, query cache and a shared daily budget; query engine and `deals find`; category signal and verdict log in the batch run | Built |
| 2. Service and agents | HTTP API, MCP server and `/healthz` in a long-running container from the same image, sharing the SQLite file; write tools for the watchlist and the shopping list; deploy config in the homelab repo | Planned |
| 3. Home Assistant | REST sensor and an image card in the existing Angebote tab of the Overview dashboard, an Assist sentence, an actionable push | Planned |
| 4. Refinement | MCP Apps cards in Claude Desktop, `price_history`, the keep-or-drop decision on the LLM tier | Planned |

### Layout

One image, one console script `deals`:

- `deals run-watchlist` is the image's default command, so the systemd timer
  in `deploy/` runs it unchanged: twice weekly, `docker run --rm`, the local
  LLM inside. It produces the same to-do updates and pushes as before.
- `deals find <query> [--when now|week|upcoming] [--json|--speech]` answers one
  question from the command line.

| Module | Role |
|---|---|
| `marktguru.py` | Marktguru client: normalization, key cache, query cache, budget, fallback |
| `store.py` | SQLite store, schema migration, retention |
| `query.py` | `when` filter, grouping, validity labels, text and speech renderings |
| `match.py` | Category signal, then rapidfuzz |
| `llm_escalate.py` | Local model for the uncertain middle, batch only |
| `ha.py` | Home Assistant REST calls |
| `watchlist_run.py` | The batch run |
| `cli.py` | Command line |

### Binding decisions

- **Store (decision 3): SQLite on `raspi4-02` local disk.** Tables `offers`
  (every fetched offer, upserted on each fetch), `fetches` plus `fetch_offers`
  (one row per live search and the offers it returned), `matches` (every
  watchlist verdict), the unchanged `notified_offers` dedup table, and `kv`
  for the cached Marktguru keys. WAL mode and a busy timeout let a second
  process share the file; multi-statement writes take the lock up front.
  Opening a v1 database migrates it in place (`PRAGMA user_version` 2). v1
  stored `valid_from` as the UTC date of Marktguru's start instant, one day
  early; v2 reads validity in local time and re-dates the v1 dedup rows, so
  nothing already pushed is pushed again.
  - Retention: offers 12 months after they ended, `raw_json` 30 days after
    the offer was last fetched, the fetch log 90 days, verdicts 12 months.
    The watchlist run prunes at its start.
  - Backup: a nightly copy to the NAS like `mealie-backup`. This is a
    homelab deploy concern and lands with phase 2.
- **Marktguru traffic (decision 4): live search on demand with cache and daily
  cap.** The terms prohibit automated extraction, so traffic stays at "one
  person browsing" scale:
  - the frontend keys are cached for about 6 hours;
  - a search for the same normalized query within 12 hours is answered from
    the store without a request;
  - at most 30 live searches per local day, shared by every caller including
    the watchlist run and counted in `fetches`. Failed searches and the retry
    after a key refresh count too;
  - once the budget is spent or Marktguru fails, the answer comes from the
    newest stored result for that query and states its fetch age;
  - no speculative prefetch, the identifying `User-Agent` stays, and only the
    deal host talks to Marktguru.
- **Answers.** `when` is `now` (valid today; on a Sunday, the next opening
  day, Monday; plus offers starting within 7 days as "coming up"), `week`
  (valid at some point in the next 7 days) or `upcoming` (starts later). The
  category prefilter runs first, then rapidfuzz. Results are grouped by
  product, sorted by price then unit price, with current and upcoming offers
  listed separately. Every offer states its validity span with weekday and
  date, as in "butter is 0.99 EUR until Sat 24 Oct at REWE". Every result
  carries `fetched_at`, `from_cache` and `budget_left`. A short German speech
  rendering serves the later Home Assistant Assist path.
- **Matching (decision 11): keep the local LLM tier for the batch, add a
  category signal, log verdicts, decide after about 4 weeks.** When the
  watchlist item names a Marktguru category ("Butter"), offers in other
  categories (Butterkäse, Butterkekse, Stollen) are rejected before rapidfuzz.
  The category never accepts an offer on its own, because margarine and
  spreads also sit in "Butter". Every verdict lands in `matches` with its
  method (`fuzzy`, `category` or `llm`); the logged escalation rate decides
  whether the model stays. Interactive answers carry no model: the calling
  agent judges the candidates.
- **No public connector (decision 7): "revisit on concrete need".** The
  service stays LAN-only; see "Future options".

### Future options

Recorded so they are not lost, each with the trigger to revisit it.

- **Option C, a public connector** ("might be an option later"): expose the
  MCP endpoint through Tailscale Funnel or a Cloudflare Tunnel, put OAuth in
  front, and register it as a claude.ai custom connector. Deal questions would
  then work in claude.ai web, natively in Claude Desktop, and in the Claude
  mobile app. It costs a public endpoint into the homelab, an OAuth server to
  run, and a worse ToS posture, since a public tool reads as "offered as a
  service" and lets others drive its traffic. Trigger: wanting deal answers
  from the Claude mobile app rather than the Home Assistant app.

| Item | Trigger to revisit |
|---|---|
| HACS custom integration for Home Assistant | The REST-sensor card proves limiting |
| Home Assistant LLM conversation agent | Free-form phone questions are wanted, at the cost of a paid API key |
| kaufda.de as a second source | Marktguru misses a chain the household uses |
| Prefetching a fixed list of staples | The store shows the same staples being asked every week |

---

**Purpose of the sections below:** the original standalone handoff brief for
an **implementation agent** with no prior conversation context, kept as the
record of the batch job's design. It contains the research background,
the concrete decisions made against this specific homelab, and an ordered build
plan. Everything under "Decisions" is settled — do not re-litigate it without a
concrete blocker; everything under "Open questions before build" must be
confirmed (usually by checking live state) before writing code that depends on it.

---

## Goal

One self-hosted tool that watches weekly supermarket flyers ("Prospekte"/"Angebote")
across the major German grocery chains for user-defined watchlist items, and sends a
notification when a watched item goes on sale — without the ad-laden third-party apps
(Marktguru app, kaufDA app, etc.) that provide this today but harvest user data and
inject advertising.

---

## Decisions

Locked during the planning session. Each entry states the decision and why, so a
later agent can judge whether a changed constraint still supports it.

Superseded on 2026-08-21: the target node, the k3s deployment shape, the k8s
CronJob scheduler, the k8s Secret for the token and the `local-path` PVC. The
job now runs on `raspi4-02` from a systemd timer, reads the token from an env
file, and keeps SQLite on a host directory mounted into the container (see
"Deployment layout"). The entries are kept as written for the reasoning.

- **Target node: `the target host`** (k3s agent, 3.7GB RAM, the homelab's established
  "apps node" — see `ARCHITECTURE.md` §3, §5.2). Checked live at planning time:
  58% RAM used (Mealie 438Mi, Paperless 653Mi, Postgres 85Mi, Redis 8Mi, Prometheus
  328Mi), ~1.5GB free. A twice-weekly CronJob workload with no standing pod between
  runs fits comfortably. `homeassistant` (HAOS, off-limits to non-HA roles — a prior
  HA-add-on Paperless instance drifted for two weeks before being retired, see
  `ARCHITECTURE.md` §6.8) and `magicmirror` (chronically near its RAM ceiling) were
  considered and rejected: technically capable of hosting a workload this light, but
  no benefit over `the target host` and each carries its own documented reason to stay
  single-purpose. Revisit only if a concrete reason for isolation from the cluster
  ever comes up.

- **Deployment shape: k3s, not Docker Compose.** Namespace `deal-scraper`, structured
  like `k8s/mealie/` (`00-namespace.yaml`, `bootstrap.sh`, a CronJob manifest,
  `nodeSelector: {homelab.lan/role: apps}`, explicit `resources.requests`). Matches
  the homelab's established pattern (`paperless-backup`, `mealie-backup` are already
  CronJobs) rather than introducing a second orchestration mechanism for one small
  workload.

- **Scheduler: native k8s CronJob**, schedule aligned to Marktguru's Mon/Thu refresh
  cadence. No separate APScheduler/cron container — the CronJob spins up a pod at
  the scheduled time, runs the fetch→normalize→match→notify pipeline to completion,
  and the pod disappears. No idle resource use between runs.

- **LLM escalation tier: kept, but never a standing service.** The tiny local model
  (see compute budget table below) is bundled *inside the matcher container image*
  (llama.cpp binary + a ~230M–1B quantized model file) and invoked as a conditional
  subprocess only when the deterministic fuzzy-match step flags a result as
  uncertain. No separate Deployment, no separate schedule, no idle RAM cost — the
  model loads, answers, and the process exits within the same CronJob run. This
  was the specific point the planning session resolved: "only run the LLM when
  actually needed" is achieved by conditional in-process invocation, not by trying
  to schedule the model itself.

- **Watchlist source: a Home Assistant `todo` list**, maintained ad hoc by the user
  (HA app, voice, or dashboard) rather than a hand-edited alias file in this repo.
  The list exists: **"Lebensmittel Prospekt Tracker"**. Before each matching run,
  the scraper calls the HA REST API service `todo.get_items` (with
  `return_response`) against that entity and fuzzy-matches offer titles directly
  against each item's `summary` text. This replaces the "hand-maintained alias
  table" step from the original research — a per-item alias table may still be
  worth adding later as a fallback for bundle listings ("Fanta/Sprite/Mezzo Mix
  versch. Sorten"), but is not needed for a first version.

- **Notifier: direct HA push, no MQTT, no intermediate automation.** After finding a
  genuinely new match (post-dedup), the CronJob pod makes one HTTP call to HA's REST
  API: `POST /api/services/notify/mobile_app_<device>` with the message text. This
  is the simplest direct path per this homelab's own operating principle (prefer
  the simplest end-to-end path for infrequent/low-volume operational work over
  building an automation layer) and needs no new HA automation to exist. An HA
  automation in between remains a possible later addition (quiet hours, multi-device
  fan-out) but is not needed for a first version.

- **Auth: one HA Long-Lived Access Token**, used for both the `todo.get_items` read
  and the `notify.*` push call, stored as a k8s Secret in the `deal-scraper`
  namespace. Follow the `paperless-secrets` pattern exactly (`k8s/paperless/bootstrap.sh`):
  `bootstrap.sh` runs `kubectl create secret generic deal-scraper-secrets
  --from-literal=HA_TOKEN="${HA_TOKEN:?...}"` imperatively — the token is never
  written into a committed YAML file, only ever passed via an env var at bootstrap
  time. A token has already been minted for testing; it will be rotated once initial
  testing is done, so do not treat the one used during development as long-lived.

- **Store: SQLite on a `local-path` PVC**, dedup table keyed on offer ID + validity
  window, same pattern as Mealie's SQLite-on-PVC deployment. Write volume is twice
  weekly — negligible SD-card wear, no different from precedent already accepted
  elsewhere in this cluster.

- **ToS / undocumented-API risk: explicitly accepted by the user.** Marktguru/kaufda
  ToS almost certainly don't authorize this kind of automated access, and the
  underlying APIs can change without notice (the `rewe-discounts` project breaking
  after REWE added a certificate requirement is a live precedent for the fragility,
  not just a theoretical risk). No technical mitigation changes this — it's a
  standing risk of the chosen approach, noted here so it isn't silently forgotten.

---

## Open questions before build

All resolved as of this planning session:

- ~~Exact HA `todo` entity id for the watchlist~~ — **done.** List
  "Lebensmittel Prospekt Tracker" created; look up its entity id
  (Developer Tools → States, filter `todo.`) when wiring the `todo.get_items` call.
- ~~Whether the HA Companion App / mobile push is already set up~~ — **confirmed
  set up.**
- **Exact `notify.mobile_app_<device>` service name** still needs a quick lookup
  (Developer Tools → Actions in HA) when wiring the notifier step — not a blocker,
  just a value to fill in.

---

## Research findings: existing open-source building blocks

| Tool | What it does | License | Link |
|---|---|---|---|
| **openclaw-supermarket-deals** | Node/TS CLI. Calls Marktguru's internal JSON API directly (extracts API keys from the Marktguru homepage at runtime, no registration). Covers Aldi Nord/Süd, Lidl, REWE, EDEKA, Kaufland and more in one call. Returns product, store, price, EUR/L, validity window, direct link. Explicitly designed as a "dumb fetcher" — intended to hand results to an LLM agent for filtering. | MIT | https://github.com/benmillerat/openclaw-supermarket-deals |
| **ha-rewe** | Home Assistant custom component exposing REWE Prospekt discounts as a sensor; can drive HA automations directly. | — | https://github.com/FaserF/ha-rewe |
| **rewe-discounts** | Standalone Python/Go script hitting REWE's undocumented API. Documented case of the endpoint breaking after REWE started requiring an app certificate/private key — a concrete example of reverse-engineered-API fragility. | — | https://github.com/foo-git/rewe-discounts |
| **brochr** | Small Android app, same pattern against REWE + EDEKA's own offer APIs. | — | https://github.com/mkfrey/brochr |
| **kaufda.de embedded JSON** | kaufda.de (same parent company as Marktguru, Bonial) ships a full JSON data layer embedded in the page — parseable without a headless browser. Useful as a secondary source for branches Marktguru misses. **Not built:** the planned fallback fetcher does not exist. | n/a (site behavior, not a packaged tool) | — |
| **changedetection.io** | Generic self-hosted website/price change monitor. Docker, CSS-selector/JSONPath targeting, Apprise notification backend (85+ channels incl. ntfy, Telegram, webhook, HA-compatible webhook). Not deal-aware — no unified schema, no watchlist matching, just diffing. Useful as a fallback for individual product pages, not as the core engine. | AGPL | https://github.com/dgtlmoon/changedetection.io |

**Key insight:** Marktguru is itself an aggregator across most major German chains.
Building "one tool for all chains" mostly means building one fetcher against
Marktguru, with fallback fetchers only for the gaps it doesn't cover (some regional
branches don't submit flyers to Marktguru — openclaw's own README flags this).

## Compute budget reference (Raspberry Pi, CPU-only, llama.cpp, 2026 benchmarks)

Relevant only to the conditional LLM-escalation subprocess described above — this
never runs as a standing service, so the numbers below are about single-call
latency, not sustained load.

| Model size | Observed throughput | Fit for this task |
|---|---|---|
| ~230M (e.g. LFM2.5-230M) | ~40 tok/s, <1GB RAM | Good fit — closer to the actual task size (short classification, not open-ended generation) |
| 1–2B Q4 (e.g. Llama 3.2 1B) | ~4–15 tok/s | Fine — irrelevant latency for a weekly batch escalation step |
| 3B Q4 | ~2–5 tok/s | Workable but no real benefit over the smaller models for this task |
| 7B+ | Loads, but very slow on CPU-only ARM | Not worth it here |

No model above ~3B is worth considering for this workload.

---

## Final architecture

One container run per timer trigger, one image, four stages run in sequence per
invocation (`deals run-watchlist` still runs these; "Current design" above adds
the offer store, the shared Marktguru client and the category signal):

1. **Fetch** - HTTP JSON calls against Marktguru. Negligible compute. The
   kaufda.de fallback for gaps was planned but never built.
2. **Normalize** — maps all sources into one schema: `title, brand, price,
   unit_price, unit, retailer, plz, valid_from, valid_to, image_url, source_url`.
   Handles German number formatting (comma decimals, "1,25 l" etc.).
3. **Match** — reads the watchlist from HA (`todo.get_items`), fuzzy-matches
   (`rapidfuzz`) offer titles against watchlist item text. Uncertain matches only:
   conditional subprocess call to the bundled tiny local model.
4. **Store + notify** — SQLite dedup check (offer ID + validity window); for each
   genuinely new match, `POST` to HA's `notify.mobile_app_<device>`.

## Deployment layout

A systemd timer on `raspi4-02` starts a oneshot service, which runs the locally
built image with `docker run --rm` and exits when the run is done:

```
deploy/
├── deal-scraper.service   # oneshot: docker run --rm --env-file ... -v <host dir>:/data
├── deal-scraper.timer     # OnCalendar=Mon,Thu 08:00, Persistent=true
```

Configuration and the HA token come from an env file on the host (see
`.env.example`), never from the repo. The SQLite file lives in a host directory
mounted at `/data`. No database container, no NAS mount (the SQLite file is
disposable dedup state, not a document store), no port (nothing needs to reach
the container).

---

## Suggested build order for the implementation agent

Historical: this is the original plan. The kaufda.de fallback (step 2) was never
built, and steps 6, 8 and 9 were later replaced by the systemd timer on
`raspi4-02` described in "Deployment layout".

1. Confirm the two "Open questions" above against live HA state.
2. Scaffold the fetcher against the Marktguru API (reference: openclaw-supermarket-deals)
   plus a kaufda.de fallback fetcher.
3. Implement the normalized schema.
4. Implement the HA `todo.get_items` read and the deterministic matcher
   (rapidfuzz), tested against a small real watchlist.
5. Bundle the tiny local model + llama.cpp binary into the matcher image; wire it
   as the conditional escalation path for uncertain matches only.
6. Add the SQLite store with dedup logic (offer ID + validity window) on a
   `local-path` PVC.
7. Wire the notifier: `POST /api/services/notify/mobile_app_<device>` on new matches.
8. Package as `k8s/deal-scraper/` following the `k8s/mealie/` layout; `nodeSelector`
   pinned to `homelab.lan/role: apps`, explicit `resources.requests`.
9. Verify end-to-end on `the target host` against a real Mon/Thu Marktguru refresh before
   considering it done.
