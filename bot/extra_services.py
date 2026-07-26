"""Additional taxi services selected by the passenger before confirmation.

Requirement 1: at order time (regular taxi only, NOT delivery) the passenger
can toggle any of the services below. Baggage / animals add a configurable
fee to the final price; children / detour / waiting are just flags forwarded
to the driver. «Waiting» also signals that the passenger agrees to pay for
the in-ride paid waiting (see waiting_service.py).

All prices live in the ``settings`` table so the admin can edit them.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from common.settings_service import get_float

# key -> metadata. price_key is the settings key holding the fee (paid services).
# Requirement 6: the passenger now chooses only three services — Багаж,
# Животные, Дети. «Заезд не по пути» and «С ожиданием» were removed from the
# selection (paid waiting is still available in-ride via the driver buttons).
# Legacy keys (detour/waiting) are kept in ``_LEGACY_KEYS`` so old orders that
# already stored them still render correctly in receipts.
SERVICES: list[dict] = [
    {"key": "baggage", "label": "\U0001F9F3 \u0411\u0430\u0433\u0430\u0436", "price_key": "svc_baggage_price", "paid": True},
    {"key": "animals", "label": "\U0001F43E \u0416\u0438\u0432\u043e\u0442\u043d\u044b\u0435", "price_key": "svc_animal_price", "paid": True},
    {"key": "children", "label": "\U0001F9D2 \u0414\u0435\u0442\u0438", "price_key": None, "paid": False},
]

SERVICE_KEYS = [s["key"] for s in SERVICES]
_BY_KEY = {s["key"]: s for s in SERVICES}


def blank_selection() -> dict[str, bool]:
    return {k: False for k in SERVICE_KEYS}


def toggle(selection: dict, key: str) -> dict:
    selection = dict(selection or {})
    selection[key] = not selection.get(key, False)
    return selection


def price(session: Session, price_key: str) -> float:
    return get_float(session, price_key, 50)


def service_price(session: Session, key: str) -> float:
    meta = _BY_KEY.get(key)
    if not meta or not meta["paid"] or not meta["price_key"]:
        return 0.0
    return price(session, meta["price_key"])


def extras_cost(session: Session, selection: dict) -> float:
    """Total fee of the paid services currently selected (baggage + animals)."""
    total = 0.0
    for meta in SERVICES:
        if meta["paid"] and (selection or {}).get(meta["key"]):
            total += price(session, meta["price_key"])
    return round(total, 2)


def any_selected(selection: dict) -> bool:
    return any((selection or {}).get(k) for k in SERVICE_KEYS)


def waiting_requested(selection: dict) -> bool:
    return bool((selection or {}).get("waiting"))


def describe(session: Session, selection: dict, with_prices: bool = False) -> list[str]:
    """Human-readable lines for the selected services (for driver / confirmation)."""
    lines: list[str] = []
    for meta in SERVICES:
        if not (selection or {}).get(meta["key"]):
            continue
        label = meta["label"]
        if meta["paid"]:
            if with_prices:
                label += f" \u2014 +{price(session, meta['price_key']):.0f} \u20bd"
        elif meta["key"] == "children":
            label += " (\u0431\u0435\u0437 \u0434\u043e\u043f\u043b\u0430\u0442\u044b)"
        elif meta["key"] == "detour":
            label += " (\u043f\u043e \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0451\u043d\u043d\u043e\u0441\u0442\u0438 \u0441 \u0432\u043e\u0434\u0438\u0442\u0435\u043b\u0435\u043c)"
        elif meta["key"] == "waiting":
            label += " (\u0433\u043e\u0442\u043e\u0432 \u043e\u043f\u043b\u0430\u0442\u0438\u0442\u044c \u043e\u0436\u0438\u0434\u0430\u043d\u0438\u0435)"
        lines.append(label)
    return lines


def to_json(selection: dict) -> str:
    # store only the selected keys to keep it compact
    active = [k for k in SERVICE_KEYS if (selection or {}).get(k)]
    return json.dumps(active, ensure_ascii=False)


def from_json(raw: str | None) -> dict[str, bool]:
    sel = blank_selection()
    if not raw:
        return sel
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return sel
    if isinstance(data, dict):
        for k in SERVICE_KEYS:
            sel[k] = bool(data.get(k))
    elif isinstance(data, list):
        for k in data:
            if k in sel:
                sel[k] = True
    return sel
