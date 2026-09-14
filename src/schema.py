"""Normalized offer schema shared by every fetcher."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Offer:
    title: str
    brand: Optional[str]
    price: Optional[float]        # EUR, e.g. 1.99
    unit_price: Optional[float]   # EUR per unit (e.g. per liter/kg), when derivable
    unit: Optional[str]           # e.g. "l", "kg", "Stk"
    retailer: str                 # e.g. "REWE", "Aldi Nord"
    plz: str                      # postal code the offer was fetched for
    valid_from: date
    valid_to: date
    image_url: Optional[str]
    source_url: Optional[str]
    offer_id: str                 # stable id from the source, used for dedup

    def to_dict(self) -> dict:
        d = asdict(self)
        d["valid_from"] = self.valid_from.isoformat()
        d["valid_to"] = self.valid_to.isoformat()
        return d


def parse_german_price(raw: str) -> Optional[float]:
    """'1,99' / '1,99 €' / '0,99' -> 1.99. Returns None if unparseable."""
    if not raw:
        return None
    cleaned = raw.strip().replace("€", "").replace("\xa0", " ").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def parse_german_unit(raw: str) -> tuple[Optional[float], Optional[str]]:
    """'1,25 l' -> (1.25, 'l'). Best-effort; returns (None, None) if unparseable."""
    if not raw:
        return None, None
    parts = raw.strip().split()
    if len(parts) < 2:
        return None, None
    amount = parse_german_price(parts[0])
    unit = parts[1].strip(".,")
    return amount, unit or None
