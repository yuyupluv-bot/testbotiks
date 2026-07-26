"""Fare calculation and promocode application."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from common.models import Promocode
from common.settings_service import get_float


@dataclass
class Fare:
    base: float
    distance_km: float
    price: float
    discount: float = 0.0

    @property
    def total(self) -> float:
        return max(round(self.price - self.discount, 2), 0.0)


def calculate_fare(session: Session, distance_km: float) -> Fare:
    base = get_float(session, "base_fare", 80)
    per_km = get_float(session, "price_per_km", 20)
    min_fare = get_float(session, "min_fare", 100)
    price = base + per_km * max(distance_km, 0)
    price = max(price, min_fare)
    return Fare(base=base, distance_km=distance_km, price=round(price, 2))


def apply_promocode(session: Session, fare: Fare, code: str) -> tuple[Fare, str]:
    """Return (updated fare, human message). Does not consume the code."""
    code = code.strip().upper()
    promo = (
        session.query(Promocode)
        .filter(Promocode.code == code, Promocode.is_active.is_(True))
        .one_or_none()
    )
    if promo is None:
        return fare, "Промокод не найден."
    now = dt.datetime.now(dt.timezone.utc)
    if promo.valid_until and promo.valid_until < now:
        return fare, "Срок действия промокода истёк."
    if promo.usage_limit is not None and promo.used_count >= promo.usage_limit:
        return fare, "Лимит использований промокода исчерпан."

    if promo.discount_type == "percent":
        discount = round(fare.price * float(promo.discount) / 100, 2)
    else:
        discount = float(promo.discount)
    fare.discount = min(discount, fare.price)
    return fare, f"Промокод применён: −{fare.discount:.0f} ₽"


def consume_promocode(session: Session, code: str) -> None:
    promo = session.query(Promocode).filter(Promocode.code == code.strip().upper()).one_or_none()
    if promo is not None:
        promo.used_count += 1
