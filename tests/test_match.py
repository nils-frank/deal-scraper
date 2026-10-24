from __future__ import annotations

import pytest

import match
from conftest import make_offer
from match import MatchDecision


def test_identical_title_is_confident_match():
    result = match.classify("Testmarke Frischkaese", make_offer(title="Testmarke - Frischkaese"))
    assert result.decision == MatchDecision.MATCH


def test_extra_words_in_offer_title_still_match():
    result = match.classify("Testmarke", make_offer(title="Testmarke - Frischkaese Doppelrahm versch. Sorten"))
    assert result.decision == MatchDecision.MATCH


def test_punctuation_is_ignored():
    assert match.score_offer("Brause Cola", make_offer(title="Brause-Cola")) == 100


def test_unrelated_offer_is_rejected():
    result = match.classify("Testmarke Frischkaese", make_offer(title="Fahrradhelm Sport"))
    assert result.decision == MatchDecision.NO_MATCH


@pytest.mark.parametrize(
    ("score", "decision"),
    [
        (match.CONFIDENT_THRESHOLD, MatchDecision.MATCH),
        (match.CONFIDENT_THRESHOLD - 0.1, MatchDecision.UNCERTAIN),
        (match.REJECT_THRESHOLD, MatchDecision.UNCERTAIN),
        (match.REJECT_THRESHOLD - 0.1, MatchDecision.NO_MATCH),
    ],
)
def test_threshold_boundaries(monkeypatch, score, decision):
    monkeypatch.setattr(match, "score_offer", lambda summary, offer: score)
    assert match.classify("anything", make_offer()).decision == decision


def test_classify_all_keeps_offer_order():
    offers = [make_offer(offer_id="1"), make_offer(offer_id="2")]
    results = match.classify_all("Testmarke", offers)
    assert [r.offer.offer_id for r in results] == ["marktguru:1", "marktguru:2"]
