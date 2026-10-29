"""Deterministic matching of Marktguru offers against a watchlist item or query.

Two stages: Marktguru's product category first, then rapidfuzz on the title.
The category only rules offers out: when some offer's category is named by the
item ("Butter", "Bio Butter"), offers in other categories (Butterkäse, Butterkekse)
are rejected. It never rules one in, because a category is not exact (margarine
and spreads sit in "Butter"), and brand-only items ("Philadelphia") name no
category, so for them it gives no signal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz, utils

from deals.schema import Offer

log = logging.getLogger(__name__)

# Above this score: confident match, no LLM needed.
CONFIDENT_THRESHOLD = 82
# Below this score: confident non-match, no LLM needed.
REJECT_THRESHOLD = 55


class MatchDecision(Enum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNCERTAIN = "uncertain"


class MatchMethod(Enum):
    FUZZY = "fuzzy"
    CATEGORY = "category"
    LLM = "llm"


@dataclass(frozen=True)
class MatchResult:
    watchlist_summary: str
    offer: Offer
    score: float
    decision: MatchDecision
    method: MatchMethod


def _words(text: str) -> str:
    return " ".join(utils.default_process(text).split())


def score_offer(watchlist_summary: str, offer: Offer) -> float:
    # token_set_ratio ignores word order and extra words on either side, which fits
    # German flyer titles like "Fanta/Sprite/Mezzo Mix versch. Sorten" matched against
    # a plain watchlist entry like "Fanta". utils.default_process strips punctuation
    # before tokenizing - without it, "Coca-Cola" (hyphenated, one token) vs a
    # watchlist "Coca Cola" (two tokens) scores as barely related instead of identical.
    return fuzz.token_set_ratio(watchlist_summary, offer.title, processor=utils.default_process)


def target_categories(watchlist_summary: str, offers: list[Offer]) -> frozenset[str]:
    """The categories among `offers` that the item names, as a whole or as one of
    its words, ignoring case and punctuation. Empty when it names none."""
    item = _words(watchlist_summary)
    names = {item, *item.split()}
    return frozenset(o.category for o in offers if o.category and _words(o.category) in names)


def classify(watchlist_summary: str, offer: Offer, categories: frozenset[str] = frozenset()) -> MatchResult:
    score = score_offer(watchlist_summary, offer)
    method = MatchMethod.FUZZY
    if categories and offer.category and offer.category not in categories:
        decision, method = MatchDecision.NO_MATCH, MatchMethod.CATEGORY
    elif score >= CONFIDENT_THRESHOLD:
        decision = MatchDecision.MATCH
    elif score < REJECT_THRESHOLD:
        decision = MatchDecision.NO_MATCH
    else:
        decision = MatchDecision.UNCERTAIN
    log.info(
        "classify item=%r offer=%r (offer_id=%s category=%r) score=%.1f -> %s by %s",
        watchlist_summary, offer.title, offer.offer_id, offer.category, score, decision.value, method.value,
    )
    return MatchResult(watchlist_summary=watchlist_summary, offer=offer, score=score, decision=decision, method=method)


def classify_all(watchlist_summary: str, offers: list[Offer]) -> list[MatchResult]:
    categories = target_categories(watchlist_summary, offers)
    if categories:
        log.info("Category signal for %r: %s", watchlist_summary, sorted(categories))
    return [classify(watchlist_summary, offer, categories) for offer in offers]
