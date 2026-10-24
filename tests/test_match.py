from __future__ import annotations

import pytest

from conftest import make_offer
from deals import match
from deals.match import MatchDecision, MatchMethod


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


def test_category_named_by_item_rules_out_other_categories():
    offers = [
        make_offer(offer_id="1", title="Testmarke Butter", category="Butter"),
        make_offer(offer_id="2", title="Testmarke Butter-Kekse", category="Kekse"),
        make_offer(offer_id="3", title="Butter Ohne Kategorie"),
    ]
    assert match.target_categories("Bio Butter", offers) == {"Butter"}
    results = match.classify_all("Butter", offers)
    assert [(r.decision, r.method) for r in results] == [
        (MatchDecision.MATCH, MatchMethod.FUZZY),
        (MatchDecision.NO_MATCH, MatchMethod.CATEGORY),
        (MatchDecision.MATCH, MatchMethod.FUZZY),
    ]


def test_category_never_rules_in():
    offer = make_offer(title="Halbfettmargarine", category="Butter")
    [result] = match.classify_all("Butter", [offer])
    assert (result.decision, result.method) == (MatchDecision.NO_MATCH, MatchMethod.FUZZY)


def test_brand_item_gives_no_category_signal():
    offers = [
        make_offer(offer_id="1", title="Testmarke Frischkaese", category="Frischkaese"),
        make_offer(offer_id="2", title="Testmarke Kuchen", category="Kuchen & Feinbackwaren"),
    ]
    assert match.target_categories("Testmarke", offers) == frozenset()
    assert [r.decision for r in match.classify_all("Testmarke", offers)] == [MatchDecision.MATCH, MatchDecision.MATCH]


def test_category_names_compare_without_case_or_punctuation():
    offers = [make_offer(category="Kuchen & Feinbackwaren")]
    assert match.target_categories("kuchen feinbackwaren", offers) == {"Kuchen & Feinbackwaren"}
