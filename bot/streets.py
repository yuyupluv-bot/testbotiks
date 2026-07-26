"""Fuzzy street lookup against the `streets` table.

Uses difflib (stdlib) so there are no extra dependencies. Returns best matches
to help the passenger confirm ambiguous input.
"""
from __future__ import annotations

import difflib

from sqlalchemy.orm import Session

from common.models import Street


def load_streets(session: Session, city_id: int | None) -> list[str]:
    query = session.query(Street.name)
    if city_id is not None:
        query = query.filter((Street.city_id == city_id) | (Street.city_id.is_(None)))
    return [row[0] for row in query.all()]


def fuzzy_match(query: str, streets: list[str], limit: int = 3, cutoff: float = 0.5) -> list[str]:
    """Return up to `limit` closest street names for `query`."""
    query_norm = query.strip().lower()
    if not query_norm or not streets:
        return []

    # substring matches first (most intuitive for users)
    substrings = [s for s in streets if query_norm in s.lower()]
    if substrings:
        return substrings[:limit]

    matches = difflib.get_close_matches(
        query, streets, n=limit, cutoff=cutoff
    )
    return matches


def best_address(query: str, streets: list[str]) -> str:
    """Return the single best street guess, or the raw query if none."""
    matches = fuzzy_match(query, streets, limit=1, cutoff=0.6)
    return matches[0] if matches else query.strip()
