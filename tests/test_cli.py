from __future__ import annotations

import json
from datetime import date

import pytest

from conftest import NOW, FakeMarktguru, raw_offer
from deals import cli, marktguru, watchlist_run


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("DEAL_SCRAPER_PLZ", "00000")
    monkeypatch.setattr(cli, "utcnow", lambda: NOW)
    fake = FakeMarktguru()
    fake.results["Frischkaese"] = [
        raw_offer(1, brand="Testmarke", product="Frischkaese", retailer="Markt A", price=1.11),
        raw_offer(2, brand="Testmarke", product="Frischkaese", retailer="Markt B", price=0.99,
                  valid_from=date(2026, 10, 26), valid_to=date(2026, 10, 31)),
    ]
    monkeypatch.setattr(marktguru, "new_session", lambda: fake)
    return fake


def test_find_prints_grouped_result(fake, capsys):
    assert cli.main(["find", "Frischkaese"]) == 0
    assert capsys.readouterr().out.splitlines()[:5] == [
        "Frischkaese on offer today, Thu 22 Oct:",
        "- Testmarke Frischkaese: 1.11 € at Markt A, until Sat 24 Oct",
        "",
        "Coming up in the next days:",
        "- Testmarke Frischkaese: 0.99 € at Markt B, from Mon 26 Oct until Sat 31 Oct",
    ]


def test_find_upcoming_as_json(fake, capsys):
    assert cli.main(["find", "Frischkaese", "--when", "upcoming", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["when"] == "upcoming"
    assert result["current"] == []
    assert [o["store"] for g in result["upcoming"] for o in g["offers"]] == ["Markt B"]
    assert result["budget_left"] == marktguru.DAILY_BUDGET - 1


def test_find_speech(fake, capsys):
    cli.main(["find", "Frischkaese", "--speech"])
    assert capsys.readouterr().out.strip() == (
        "Frischkaese im Angebot: Testmarke Frischkaese 1,11 € bei Markt A bis Samstag. "
        "Demnächst: ab Montag Testmarke Frischkaese 0,99 € bei Markt B."
    )


def test_find_needs_postal_code(fake, monkeypatch, capsys):
    monkeypatch.delenv("DEAL_SCRAPER_PLZ")
    assert cli.main(["find", "Frischkaese"]) == 2
    assert "DEAL_SCRAPER_PLZ" in capsys.readouterr().err
    assert fake.calls == []


def test_run_watchlist_dispatches(monkeypatch):
    monkeypatch.setattr(watchlist_run, "run", lambda: 0)
    assert cli.main(["run-watchlist"]) == 0
