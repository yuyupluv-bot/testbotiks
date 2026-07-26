"""Distance / duration calculation via Yandex or Google APIs.

Gracefully degrades to a haversine estimate when no API key is configured or
the request fails, so the bot keeps working in development.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import requests

from common.config import config
from common.logger import get_logger

log = get_logger("bot.maps")


@dataclass
class RouteInfo:
    distance_km: float
    duration_min: float
    estimated: bool = False


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


def geocode(address: str, city: str | None = None) -> tuple[float, float] | None:
    """Geocode an address to (lat, lon) using Yandex Geocoder."""
    if not config.MAPS_API_KEY:
        return None
    query = f"{city}, {address}" if city else address
    try:
        if config.MAPS_PROVIDER == "yandex":
            resp = requests.get(
                "https://geocode-maps.yandex.ru/1.x/",
                params={"apikey": config.MAPS_API_KEY, "geocode": query, "format": "json"},
                timeout=10,
            )
            resp.raise_for_status()
            members = resp.json()["response"]["GeoObjectCollection"]["featureMember"]
            if not members:
                return None
            pos = members[0]["GeoObject"]["Point"]["pos"]
            lon, lat = map(float, pos.split())
            return lat, lon
        else:  # google
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": query, "key": config.MAPS_API_KEY},
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("results")
            if not results:
                return None
            loc = results[0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
    except Exception as exc:  # noqa: BLE001
        log.warning("Geocode failed for '%s': %s", query, exc)
        return None


def route(
    address_from: str,
    address_to: str,
    city: str | None = None,
) -> RouteInfo:
    """Return distance/duration between two addresses."""
    a = geocode(address_from, city)
    b = geocode(address_to, city)

    if a and b and config.MAPS_API_KEY and config.MAPS_PROVIDER == "yandex":
        try:
            resp = requests.get(
                "https://api.routing.yandex.net/v2/distancematrix",
                params={
                    "origins": f"{a[0]},{a[1]}",
                    "destinations": f"{b[0]},{b[1]}",
                    "mode": "driving",
                    "apikey": config.MAPS_API_KEY,
                },
                timeout=15,
            )
            resp.raise_for_status()
            element = resp.json()["rows"][0]["elements"][0]
            if element.get("status") == "OK":
                return RouteInfo(
                    distance_km=round(float(element["distance"]["value"]) / 1000, 2),
                    duration_min=round(float(element["duration"]["value"]) / 60, 1),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Yandex distance matrix failed: %s", exc)

    if a and b and config.MAPS_API_KEY and config.MAPS_PROVIDER == "google":
        try:
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/distancematrix/json",
                params={
                    "origins": f"{a[0]},{a[1]}",
                    "destinations": f"{b[0]},{b[1]}",
                    "key": config.MAPS_API_KEY,
                    "mode": "driving",
                },
                timeout=10,
            )
            resp.raise_for_status()
            el = resp.json()["rows"][0]["elements"][0]
            if el.get("status") == "OK":
                return RouteInfo(
                    distance_km=round(el["distance"]["value"] / 1000, 2),
                    duration_min=round(el["duration"]["value"] / 60, 1),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Distance matrix failed: %s", exc)

    if a and b:
        dist = round(_haversine(a, b) * 1.3, 2)  # 1.3 road factor
        return RouteInfo(distance_km=dist, duration_min=round(dist / 40 * 60, 1), estimated=True)

    # Unknown coordinates: callers must not present a made-up distance.
    return RouteInfo(distance_km=0.0, duration_min=0.0, estimated=True)
