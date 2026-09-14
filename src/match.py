"""Deterministic fuzzy matching of Marktguru offer titles against HA todo item text."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz, utils

from schema import Offer

log = logging.getLogger(__name__)

# Above this score: confident match, no LLM needed.
CONFIDENT_THRESHOLD = 82
# Below this score: confident non-match, no LLM needed.
REJECT_THRESHOLD = 55


class MatchDecision(Enum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class MatchResult:
    watchlist_summary: str
    offer: Offer
    score: float
    decision: MatchDecision


def score_offer(watchlist_summary: str, offer: Offer) -> float:
    # token_set_ratio ignores word order and extra words on either side, which fits
    # German flyer titles like "Fanta/Sprite/Mezzo Mix versch. Sorten" matched against
    # a plain watchlist entry like "Fanta". utils.default_process strips punctuation
    # before tokenizing - without it, "Coca-Cola" (hyphenated, one token) vs a
    # watchlist "Coca Cola" (two tokens) scores as barely related instead of identical.
    return fuzz.token_set_ratio(watchlist_summary, offer.title, processor=utils.default_process)


def classify(watchlist_summary: str, offer: Offer) -> MatchResult:
    score = score_offer(watchlist_summary, offer)
    if score >= CONFIDENT_THRESHOLD:
        decision = MatchDecision.MATCH
    elif score < REJECT_THRESHOLD:
        decision = MatchDecision.NO_MATCH
    else:
        decision = MatchDecision.UNCERTAIN
    log.info(
        "classify item=%r offer=%r (offer_id=%s) score=%.1f -> %s",
        watchlist_summary, offer.title, offer.offer_id, score, decision.value,
    )
    return MatchResult(watchlist_summary=watchlist_summary, offer=offer, score=score, decision=decision)


def classify_all(watchlist_summary: str, offers: list[Offer]) -> list[MatchResult]:
    return [classify(watchlist_summary, offer) for offer in offers]
