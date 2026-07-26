"""Tiny helper so bot-side services can write to the admin action log.

Requirement 9: log all actions for the admin. The web panel already logs its
own actions via ``web.app.log_action``; this lets the long-poll bot record
events (driver blocks, fake calls, delivery price offers, …) into the same
``admin_logs`` table so everything is visible on the «Логи» page.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from common.logger import get_logger
from common.models import AdminLog

log = get_logger("bot.audit")


def record(session: Session, action: str, details: str = "") -> None:
    """Persist an audit entry (admin_id is NULL → «система/бот»)."""
    try:
        session.add(AdminLog(admin_id=None, action=action, details=details))
        session.flush()
    except Exception as exc:  # noqa: BLE001 - never break a user flow on logging
        log.warning("audit record failed (%s): %s", action, exc)
    log.info("[audit] %s | %s", action, details)
