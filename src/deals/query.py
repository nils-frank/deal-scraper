"""Query engine: "where can I get butter right now?"

find_offers searches through the shared Marktguru client (cache, budget,
fallback), keeps the offers that match the query (category, then rapidfuzz),
filters them by `when`, groups them by product and sorts by price, then unit
price. Current and upcoming offers are listed separately, every offer carries
its validity span, and every result says when its offers were fetched.

Read-only: nothing here writes notified_offers or the match log, so asking a
question never changes what the watchlist run pushes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from deals.marktguru import BUDGET_USED_UP, DAILY_BUDGET, Client
from deals.match import MatchDecision, MatchMethod, classify_all
from deals.schema import Offer, local_date

WHEN_CHOICES = ("now", "week", "upcoming")
# "now" lists offers starting within this many days as "coming up"; "week" is
# everything valid at some point in the next HORIZON_DAYS days, today included.
HORIZON_DAYS = 7
DEFAULT_LIMIT = 20
# The spoken answer stays short (D7: 3 to 5 lines).
SPEECH_CURRENT = 4
SPEECH_UPCOMING = 2

WEEKDAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WEEKDAYS_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WEEKDAYS_DE_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTHS_DE = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


@dataclass(frozen=True)
class OfferGroup:
    name: str
    offers: list[Offer]  # cheapest first


@dataclass(frozen=True)
class FindResult:
    query: str
    when: str
    today: date
    reference_day: date  # the day "now" means: today, or Monday when today is a Sunday
    current: list[OfferGroup]
    upcoming: list[OfferGroup]
    fetched_at: Optional[datetime]
    source: str  # "live", "cache" or "store"
    budget_left: int
    daily_budget: int = DAILY_BUDGET
    error: Optional[str] = None
    excluded_categories: dict[str, int] = field(default_factory=dict)

    @property
    def from_cache(self) -> bool:
        return self.source != "live"

    def to_dict(self) -> dict:
        def group_dict(group: OfferGroup) -> dict:
            return {
                "name": group.name,
                "offers": [
                    {**offer.to_dict(), "validity": validity_span(offer, self.today, "de")} for offer in group.offers
                ],
            }

        return {
            "query": self.query,
            "when": self.when,
            "today": self.today.isoformat(),
            "reference_day": self.reference_day.isoformat(),
            "current": [group_dict(g) for g in self.current],
            "upcoming": [group_dict(g) for g in self.upcoming],
            "excluded_categories": self.excluded_categories,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "from_cache": self.from_cache,
            "source": self.source,
            "budget_left": self.budget_left,
            "error": self.error,
        }


def reference_day(today: date) -> date:
    """Shops are closed on Sundays, so on a Sunday "now" means Monday."""
    return today + timedelta(days=1) if today.weekday() == 6 else today


def split_by_when(offers: list[Offer], when: str, today: date) -> tuple[list[Offer], list[Offer]]:
    """(current, upcoming) for `when`:
    - now: valid on the reference day; coming up: starts within HORIZON_DAYS after it;
    - week: valid at some point from today through today + HORIZON_DAYS - 1, split
      the same way at the reference day;
    - upcoming: nothing current; everything that starts after the reference day."""
    if when not in WHEN_CHOICES:
        raise ValueError(f"when must be one of {WHEN_CHOICES}, not {when!r}")
    ref = reference_day(today)
    current = [] if when == "upcoming" else [o for o in offers if o.valid_from <= ref <= o.valid_to]
    if when == "upcoming":
        upcoming = [o for o in offers if o.valid_from > ref]
    else:
        last = (ref if when == "now" else today) + timedelta(days=HORIZON_DAYS - 1)
        upcoming = [o for o in offers if ref < o.valid_from <= last]
    return current, upcoming


def _price_key(offer: Offer) -> tuple:
    return (
        offer.price is None, offer.price or 0.0,
        offer.unit_price is None, offer.unit_price or 0.0,
        offer.valid_from, offer.retailer,
    )


def group_offers(offers: list[Offer], limit: int = DEFAULT_LIMIT) -> list[OfferGroup]:
    """Groups by brand and product name; groups and their offers cheapest first."""
    groups: dict[str, list[Offer]] = {}
    for offer in sorted(offers, key=_price_key):
        groups.setdefault(offer.name.casefold(), []).append(offer)
    return [OfferGroup(name=members[0].name, offers=members) for members in groups.values()][:limit]


def find_offers(
    client: Client,
    query: str,
    when: str = "now",
    *,
    stores: Optional[list[str]] = None,
    limit: int = DEFAULT_LIMIT,
    today: Optional[date] = None,
) -> FindResult:
    today = today or local_date(client.clock())
    search = client.search_narrowing(query)

    results = classify_all(query, search.offers)
    excluded = Counter(r.offer.category for r in results if r.method == MatchMethod.CATEGORY)
    relevant = [r.offer for r in results if r.decision != MatchDecision.NO_MATCH]
    if stores:
        wanted = {s.casefold() for s in stores}
        relevant = [o for o in relevant if o.retailer.casefold() in wanted]

    current, upcoming = split_by_when(relevant, when, today)
    return FindResult(
        query=query,
        when=when,
        today=today,
        reference_day=reference_day(today),
        current=group_offers(current, limit),
        upcoming=group_offers(upcoming, limit),
        fetched_at=search.fetched_at,
        source=search.source,
        budget_left=search.budget_left,
        daily_budget=client.daily_budget,
        error=search.error,
        excluded_categories=dict(excluded.most_common()),
    )


# --- labels ---


def _day_en(day: date) -> str:
    return f"{WEEKDAYS_EN[day.weekday()]} {day.day} {MONTHS_EN[day.month - 1]}"


def _day_de(day: date) -> str:
    return f"{WEEKDAYS_DE[day.weekday()]} {day.day:02d}.{day.month:02d}."


def _spoken_day(day: date, today: date) -> str:
    if day == today:
        return "heute"
    if day == today + timedelta(days=1):
        return "morgen"
    if today < day < today + timedelta(days=7):
        return WEEKDAYS_DE_LONG[day.weekday()]
    return f"{day.day}. {MONTHS_DE[day.month - 1]}"


# style: (day wording, "from start until end", "only on start", "until end")
_SPAN_WORDS = {
    "en": (lambda day, today: f"today ({_day_en(day)})" if day == today else _day_en(day),
           "from {start} until {end}", "only on {start}", "until {end}"),
    "de": (lambda day, today: f"heute ({_day_de(day)})" if day == today else _day_de(day),
           "ab {start} bis {end}", "nur am {start}", "bis {end}"),
    "spoken": (_spoken_day, "ab {start} bis {end}", "nur am {start}", "bis {end}"),
}


def validity_span(offer: Offer, today: date, style: str) -> str:
    """The validity span in every output ("en" text, "de" JSON label, "spoken" German):
    an offer starting after today gives start and end, or only its day when it runs
    for one day; an offer that already runs gives its end.
    E.g. "from Mon 26 Oct until Sat 31 Oct", "nur am Fr 23.10.", "bis heute"."""
    day, span, single, until = _SPAN_WORDS[style]
    if offer.valid_from <= today:
        return until.format(end=day(offer.valid_to, today))
    start = day(offer.valid_from, today)
    if offer.valid_from == offer.valid_to:
        # "nur morgen": "am Morgen" would mean "in the morning".
        return f"nur {start}" if start == "morgen" else single.format(start=start)
    return span.format(start=start, end=day(offer.valid_to, today))


def _subject(query: str) -> str:
    query = query.strip()
    return query[:1].upper() + query[1:]


def _join(names: list[str], conjunction: str) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {conjunction} {names[-1]}"


def _euro_en(value: float) -> str:
    return f"{value:.2f} €"


def _euro_de(value: float) -> str:
    return f"{value:.2f} €".replace(".", ",")


def _clusters(group: OfferGroup, today: date) -> list[list[Offer]]:
    """The group's offers merged by everything shown about them except the store.
    A start date only shows for offers that have not started yet."""
    clusters: dict[tuple, list[Offer]] = {}
    for offer in group.offers:
        starts = offer.valid_from if offer.valid_from > today else None
        key = (offer.price, offer.unit_price, offer.unit, offer.old_price, offer.pack_size, offer.app_only, starts, offer.valid_to)
        clusters.setdefault(key, []).append(offer)
    return list(clusters.values())


def _age_en(fetched_at: datetime, now: datetime) -> str:
    minutes = int((now - fetched_at).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hour{'s' if minutes // 60 != 1 else ''} ago"
    return f"{minutes // (24 * 60)} days ago"


def _age_de(fetched_at: datetime, now: datetime) -> str:
    minutes = int((now - fetched_at).total_seconds() // 60)
    if minutes < 1:
        return "gerade eben"
    if minutes < 60:
        return f"vor {minutes} Minute{'n' if minutes != 1 else ''}"
    if minutes < 48 * 60:
        return f"vor {minutes // 60} Stunde{'n' if minutes // 60 != 1 else ''}"
    return f"vor {minutes // (24 * 60)} Tagen"


# --- renderings ---


def _group_line_en(group: OfferGroup, today: date) -> str:
    sizes = {o.pack_size for o in group.offers}
    one_size = len(sizes) == 1
    name = " ".join(p for p in (group.name, group.offers[0].pack_size if one_size else None) if p)
    clusters = _clusters(group, today)
    labels = [validity_span(cluster[0], today, "en") for cluster in clusters]
    shared = len(clusters) > 1 and len(set(labels)) == 1
    parts = []
    for cluster, label in zip(clusters, labels):
        offer = cluster[0]
        part = _euro_en(offer.price) if offer.price is not None else "price unknown"
        details = []
        if offer.unit_price is not None and offer.unit:
            details.append(f"{_euro_en(offer.unit_price)}/{offer.unit}")
        if not one_size and offer.pack_size:
            details.append(offer.pack_size)
        if details:
            part += f" ({', '.join(details)})"
        if offer.old_price is not None:
            part += f", was {_euro_en(offer.old_price)}"
        part += f" at {_join(sorted(dict.fromkeys(o.retailer for o in cluster)), 'and')}"
        if not shared:
            part += f", {label}"
        if offer.app_only:
            part += ", app price"
        parts.append(part)
    return f"- {name}: " + "; ".join(parts) + (f"; all {labels[0]}" if shared else "")


def format_text(result: FindResult, now: datetime) -> str:
    """The grouped answer for `deals find`, shaped like DESIGN.md's example answer."""
    today, ref = result.today, result.reference_day
    subject = _subject(result.query)
    if result.when == "now":
        if ref == today:
            heading = f"{subject} on offer today, {_day_en(today)}:"
        else:
            heading = f"{subject} on offer on {_day_en(ref)} (shops are closed today):"
        coming = "Coming up in the next days:"
    elif result.when == "week":
        heading = f"{subject} on offer this week, as of {_day_en(ref)}:"
        coming = "Starting later this week:"
    else:
        heading = None
        coming = f"{subject} coming up:"

    lines = []
    if heading:
        lines.append(heading)
        lines.extend(_group_line_en(g, today) for g in result.current)
        if not result.current:
            lines.append("- nothing found")
    if result.upcoming or result.when == "upcoming":
        if lines:
            lines.append("")
        lines.append(coming)
        lines.extend(_group_line_en(g, today) for g in result.upcoming)
        if not result.upcoming:
            lines.append("- nothing found")

    lines.append("")
    if result.excluded_categories:
        others = ", ".join(f"{name} ({count})" for name, count in result.excluded_categories.items())
        lines.append(f"Left out as other categories: {others}.")
    if result.fetched_at is None:
        lines.append(f"No offers stored for this query: {result.error}.")
    elif result.error == BUDGET_USED_UP:
        lines.append(f"Marktguru not asked ({result.error}); stored offers fetched {_age_en(result.fetched_at, now)}.")
    elif result.source == "store":
        lines.append(f"{result.error}; stored offers fetched {_age_en(result.fetched_at, now)}.")
    else:
        cached = " (cached)" if result.source == "cache" else ""
        lines.append(f"Prices from Marktguru, fetched {_age_en(result.fetched_at, now)}{cached}.")
    lines.append(f"{result.budget_left} of {result.daily_budget} live searches left today.")
    return "\n".join(lines)


def _spoken_group(group: OfferGroup, today: date) -> tuple[str, Offer]:
    cluster = _clusters(group, today)[0]
    offer = cluster[0]
    price = f" {_euro_de(offer.price)}" if offer.price is not None else ""
    stores = _join(sorted(dict.fromkeys(o.retailer for o in cluster)), "und")
    return f"{group.name}{price} bei {stores}", offer


def format_speech(result: FindResult, now: datetime) -> str:
    """A short German answer for Home Assistant Assist, without the LLM:
    "Butter im Angebot: Almhof Butter 0,99 € bei Markt A, ..., alle bis heute.
    Demnächst: ab Montag bis Samstag Weidegold Butter 1,49 € bei Markt C." """
    today = result.today
    subject = _subject(result.query)
    sentences = []
    current = [_spoken_group(g, today) for g in result.current[:SPEECH_CURRENT]]
    if current:
        spans = [validity_span(offer, today, "spoken") for _, offer in current]
        if len(set(spans)) == 1:
            phrases = [text for text, _ in current]
            suffix = f", alle {spans[0]}" if len(phrases) > 1 else f" {spans[0]}"
            sentences.append(f"{subject} im Angebot: {', '.join(phrases)}{suffix}.")
        else:
            phrases = [f"{text} {span}" for (text, _), span in zip(current, spans)]
            sentences.append(f"{subject} im Angebot: {', '.join(phrases)}.")
    upcoming = [_spoken_group(g, today) for g in result.upcoming[:SPEECH_UPCOMING]]
    if upcoming:
        phrases = [f"{validity_span(offer, today, 'spoken')} {text}" for text, offer in upcoming]
        sentences.append(f"Demnächst: {', '.join(phrases)}.")
    if not sentences:
        if result.fetched_at is None and result.error:
            # Marktguru failed or the budget is spent, and nothing is stored for the query.
            sentences.append(f"Ich kann gerade nicht nach Angeboten für {subject} suchen.")
        else:
            sentences.append(f"Für {subject} habe ich gerade keine Angebote gefunden.")
    if result.source == "store" and result.fetched_at is not None:
        sentences.append(f"Stand: {_age_de(result.fetched_at, now)}.")
    return " ".join(sentences)
