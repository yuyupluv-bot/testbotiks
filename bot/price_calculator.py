"""Approximate passenger price calculation from price text or map distance."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from common.config import config
from common.models import PriceSection
from common.settings_service import get_float

from . import maps


BASE_CITY = "горнозаводск"
CITY_ALIASES = {
    "город": BASE_CITY,
    "города": BASE_CITY,
    "городу": BASE_CITY,
    "городе": BASE_CITY,
    "городом": BASE_CITY,
    "горнозаводск": BASE_CITY,
    "горнозаводска": BASE_CITY,
    "горнозаводску": BASE_CITY,
    "горнозаводске": BASE_CITY,
    "горнозаводском": BASE_CITY,
    "пашия": "пашия",
    "пашии": "пашия",
    "кусья": "кусья",
    "кусьи": "кусья",
}


@dataclass
class PriceEstimate:
    origin: str
    destination: str
    amount: float
    source: str
    distance_km: float | None = None
    details: str = ""


def parse_route(value: str) -> tuple[str, str] | None:
    """Parse mandatory «От ... до ...» / «Из ... до ...» form."""
    normalized = " ".join((value or "").split())
    match = re.fullmatch(r"(?i)(?:от|из)\s+(.+?)\s+до\s+(.+)", normalized)
    if not match:
        return None
    origin, destination = match.group(1).strip(), match.group(2).strip()
    if not origin or not destination:
        return None
    return origin, destination


def _normalize_city(value: str) -> str:
    tokens = re.findall(r"[а-яa-z-]+", (value or "").casefold().replace("ё", "е"))
    tokens = [token for token in tokens if token != "г"]
    normalized = " ".join(tokens).strip()
    if not normalized:
        return BASE_CITY
    return CITY_ALIASES.get(normalized, normalized)


def _city_variants(key: str) -> set[str]:
    variants = {key}
    alias = CITY_ALIASES.get(key)
    if alias:
        variants.add(alias)
    if key.endswith("ии"):
        variants.add(key[:-2] + "ия")
    if key.endswith("ия"):
        variants.add(key[:-2] + "ии")
    if key.endswith("ьи"):
        variants.add(key[:-2] + "ья")
    if key.endswith("ья"):
        variants.add(key[:-2] + "ьи")
    return {_normalize_city(item) for item in variants if item}


def price_city_rates(session: Session) -> dict[str, tuple[str, float]]:
    """Extract lines like «Пашия — 500 ₽» from every active price section."""
    rates: dict[str, tuple[str, float]] = {}
    rows = session.query(PriceSection).filter(PriceSection.is_active.is_(True)).all()
    pattern = re.compile(
        r"(?i)^\s*[•*]?\s*([а-яёa-z][а-яёa-z .-]{1,60}?)\s*(?:—|–|-|:|=)\s*(?:от\s*)?(\d[\d ]*)\s*(?:₽|руб)",
    )
    for row in rows:
        for line in (row.content or "").splitlines():
            match = pattern.search(line.strip())
            if not match:
                continue
            title = match.group(1).strip(" .-—–")
            key = _normalize_city(title)
            try:
                amount = float(match.group(2).replace(" ", ""))
            except ValueError:
                continue
            if key and amount > 0:
                rates[key] = (title, amount)
    return rates


def _lookup_rate(rates: dict[str, tuple[str, float]], city: str) -> tuple[str, float] | None:
    key = _normalize_city(city)
    variants = _city_variants(key)
    for variant in variants:
        if variant in rates:
            return rates[variant]
    for stored_key, value in rates.items():
        if stored_key in variants:
            return value
    for stored_key, value in rates.items():
        if any(variant.startswith(stored_key) or stored_key.startswith(variant) for variant in variants):
            return value
    return None


def estimate(session: Session, origin: str, destination: str) -> PriceEstimate | None:
    rates = price_city_rates(session)
    origin_key = _normalize_city(origin)
    destination_key = _normalize_city(destination)
    origin_rate = _lookup_rate(rates, origin)
    destination_rate = _lookup_rate(rates, destination)
    if origin_key == BASE_CITY and destination_rate:
        return PriceEstimate(
            origin=origin,
            destination=destination,
            amount=destination_rate[1],
            source="price",
            details=f"{destination_rate[0]}: {destination_rate[1]:.0f} ₽",
        )
    if destination_key == BASE_CITY and origin_rate:
        return PriceEstimate(
            origin=origin,
            destination=destination,
            amount=origin_rate[1],
            source="price",
            details=f"{origin_rate[0]}: {origin_rate[1]:.0f} ₽",
        )
    if origin_rate and destination_rate:
        amount = origin_rate[1] + destination_rate[1]
        return PriceEstimate(
            origin=origin,
            destination=destination,
            amount=amount,
            source="price",
            details=(
                f"{origin_rate[0]}: {origin_rate[1]:.0f} ₽ + "
                f"{destination_rate[0]}: {destination_rate[1]:.0f} ₽"
            ),
        )

    route = maps.route(origin, destination)
    if route.distance_km <= 0:
        return None
    rate = get_float(session, "price_calc_per_km", 35.0)
    raw_amount = route.distance_km * rate
    # Distance-based estimates are always rounded upward to the next 100 ₽:
    # 5 430 -> 5 500; 25 030 -> 25 100.
    amount = float(math.ceil(raw_amount / 100.0) * 100)
    return PriceEstimate(
        origin=origin,
        destination=destination,
        amount=amount,
        source="distance",
        distance_km=route.distance_km,
        details=(
            f"{route.distance_km:.1f} км × {rate:.0f} ₽/км = {raw_amount:.0f} ₽, "
            f"округлено вверх до {amount:.0f} ₽"
        ),
    )


def maps_ready() -> bool:
    return bool(config.MAPS_API_KEY)
