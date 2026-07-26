"""Per-line tariffs (requirement 1.2).

Each line (city) has its own tariff values, stored in the ``settings`` table
under namespaced keys so the admin panel can edit them:

    tariff:<city_id>:price_per_km
    tariff:<city_id>:min_price
    tariff:<city_id>:night_surcharge
    tariff:<city_id>:baggage_price
    tariff:<city_id>:animals_price

A global default (key without a city id, e.g. ``svc_baggage_price``) is used as
a fallback when a line has no explicit value yet. The «Прайс» button shows a
table with the values of every active line.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from common.models import City
from common.settings_service import get_float

# field -> (title, global fallback key, default value)
FIELDS: list[dict] = [
    {"field": "price_per_km", "title": "Цена за км", "unit": "₽/км", "fallback": "price_per_km", "default": 30.0},
    {"field": "min_price", "title": "Мин. стоимость", "unit": "₽", "fallback": "min_price", "default": 100.0},
    {"field": "night_surcharge", "title": "Ночной тариф", "unit": "₽", "fallback": "night_surcharge_amount", "default": 50.0},
    {"field": "baggage_price", "title": "Багаж", "unit": "₽", "fallback": "svc_baggage_price", "default": 50.0},
    {"field": "animals_price", "title": "Животные", "unit": "₽", "fallback": "svc_animal_price", "default": 50.0},
]

_BY_FIELD = {f["field"]: f for f in FIELDS}


def tariff_key(city_id: int, field: str) -> str:
    return f"tariff:{city_id}:{field}"


def get_tariff(session: Session, city_id: int, field: str) -> float:
    """Per-line value, falling back to the global default then the constant."""
    meta = _BY_FIELD.get(field)
    default = float(meta["default"]) if meta else 0.0
    # explicit per-line value first
    value = get_float(session, tariff_key(city_id, field), None)  # type: ignore[arg-type]
    if value is not None:
        return value
    if meta:
        return get_float(session, meta["fallback"], default)
    return default


def default_settings_keys() -> dict[str, str]:
    """Global fallbacks seeded into settings so the admin panel exposes them."""
    return {f["fallback"]: str(f["default"]) for f in FIELDS}


def price_table_text(session: Session) -> str:
    """Requirement 1.2: a price table listing every active line."""
    cities = (
        session.query(City)
        .filter(City.is_active.is_(True))
        .order_by(City.name)
        .all()
    )
    if not cities:
        return "Тарифы пока не настроены."
    lines = ["🏷 Прайс по линиям:\n"]
    for c in cities:
        lines.append(f"📍 {c.name}")
        for meta in FIELDS:
            value = get_tariff(session, c.id, meta["field"])
            lines.append(f"   • {meta['title']}: {value:.0f} {meta['unit']}")
        lines.append("")
    lines.append("Цена поездки окончательно определяется водителем.")
    return "\n".join(lines).strip()
