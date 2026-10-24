"""Normalized offer schema and the local-time helpers every module shares."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# Shops, flyer windows and the daily search budget all follow local time, not
# the container's UTC clock.
LOCAL_TZ = ZoneInfo(os.environ.get("DEAL_TZ", "Europe/Berlin"))

# Pack sizes below one of these units read better in the smaller one ("250 g").
_SMALLER_UNIT = {"kg": "g", "l": "ml"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_date(moment: datetime) -> date:
    return moment.astimezone(LOCAL_TZ).date()


def to_iso(moment: datetime) -> str:
    """Every timestamp in the store uses this one UTC format, so they compare as strings."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Offer:
    offer_id: str                     # "marktguru:<id>", the store's offer_key and the dedup id
    retailer: str                     # the chain, e.g. "REWE", "Aldi Nord"; never a branch address
    valid_from: date
    valid_to: date
    brand: Optional[str] = None
    product: Optional[str] = None
    description: Optional[str] = None  # flyer text, often variants and pack size
    price: Optional[float] = None      # EUR, e.g. 1.99
    old_price: Optional[float] = None  # EUR before the discount, when the flyer names one
    unit_price: Optional[float] = None  # EUR per `unit`
    unit: Optional[str] = None         # unit of unit_price and volume, e.g. "kg", "l"
    volume: Optional[float] = None     # pack content in `unit`, e.g. 0.25 (kg)
    quantity: Optional[float] = None   # packs per offer, e.g. 5 for "5 x 20 g"
    category: Optional[str] = None     # Marktguru's product category, e.g. "Butter"
    category_id: Optional[int] = None
    app_only: bool = False             # price needs the retailer's app or loyalty card
    image_url: Optional[str] = None
    source_url: Optional[str] = None
    raw: Optional[dict] = field(default=None, compare=False, repr=False)  # source record, kept for 30 days

    @property
    def title(self) -> str:
        """Brand, product name and flyer description, de-duplicated. product alone is
        often a generic bucket ("Cola", "Cola XXL"); brand and description carry the
        specifics ("Fanta/Sprite/MezzoMix Versch. Sorten") that matching needs."""
        parts = [p.strip() for p in (self.brand, self.product, self.description) if p and p.strip()]
        return " - ".join(dict.fromkeys(parts))

    @property
    def name(self) -> str:
        """Brand plus product name, the label offers are grouped and shown under."""
        brand = (self.brand or "").strip()
        product = (self.product or "").strip()
        if brand and product.lower().startswith(brand.lower()):
            return product
        return " ".join(p for p in (brand, product) if p) or self.title

    @property
    def pack_size(self) -> Optional[str]:
        if not self.volume or not self.unit:
            return None
        amount, unit = self.volume, self.unit
        if unit in _SMALLER_UNIT and amount < 1:
            amount, unit = amount * 1000, _SMALLER_UNIT[unit]
        size = f"{round(amount, 3):g} {unit}"
        if self.quantity and self.quantity > 1:
            return f"{self.quantity:g} x {size}"
        return size

    def to_dict(self) -> dict:
        return {
            "offer_key": self.offer_id,
            "store": self.retailer,
            "title": self.title,
            "name": self.name,
            "brand": self.brand,
            "product": self.product,
            "description": self.description,
            "category": self.category,
            "price": self.price,
            "old_price": self.old_price,
            "unit_price": self.unit_price,
            "unit": self.unit,
            "pack_size": self.pack_size,
            "app_only": self.app_only,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
            "image_url": self.image_url,
            "source_url": self.source_url,
        }
