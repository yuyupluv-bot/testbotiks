"""Small bounded PostgreSQL cleanup worker for sustained high traffic."""
from __future__ import annotations

import datetime as dt
import threading
import time

from common import time_utils
from common.config import config
from common.database import session_scope
from common.logger import get_logger
from common.models import Booking, Order, OutboxMessage, ProcessedEvent

log = get_logger("bot.maintenance")
_started = False
_lock = threading.Lock()
_BATCH_SIZE = 10_000


def _delete_id_batch(session, model, condition) -> int:
    ids = [
        row_id for (row_id,) in session.query(model.id)
        .filter(condition)
        .order_by(model.id.asc())
        .limit(_BATCH_SIZE)
        .all()
    ]
    if not ids:
        return 0
    return session.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)


def cleanup_once() -> tuple[int, int]:
    now = time_utils.now()
    event_cutoff = now - dt.timedelta(hours=config.PROCESSED_EVENTS_RETENTION_HOURS)
    outbox_cutoff = now - dt.timedelta(hours=config.OUTBOX_RETENTION_HOURS)
    with session_scope() as session:
        events = _delete_id_batch(
            session,
            ProcessedEvent,
            ProcessedEvent.created_at < event_cutoff,
        )
        messages = 0
        # Never delete pending/failed/sending records: only completed delivery
        # history is disposable under sustained traffic.
        ids = [
            row_id for (row_id,) in session.query(OutboxMessage.id)
            .filter(
                OutboxMessage.created_at < outbox_cutoff,
                OutboxMessage.status.in_(("sent", "cancelled")),
                ~OutboxMessage.id.in_(
                    session.query(Order.offer_outbox_id).filter(Order.offer_outbox_id.isnot(None))
                ),
                ~OutboxMessage.id.in_(
                    session.query(Order.departure_prompt_outbox_id).filter(Order.departure_prompt_outbox_id.isnot(None))
                ),
                ~OutboxMessage.id.in_(
                    session.query(Order.chat_notice_outbox_id).filter(Order.chat_notice_outbox_id.isnot(None))
                ),
                ~OutboxMessage.id.in_(
                    session.query(Booking.chat_notice_outbox_id).filter(Booking.chat_notice_outbox_id.isnot(None))
                ),
            )
            .order_by(OutboxMessage.id.asc())
            .limit(_BATCH_SIZE)
            .all()
        ]
        if ids:
            messages = session.query(OutboxMessage).filter(
                OutboxMessage.id.in_(ids)
            ).delete(synchronize_session=False)
    return events, messages


def _worker() -> None:
    while True:
        time.sleep(config.MAINTENANCE_INTERVAL)
        try:
            events, messages = cleanup_once()
            if events or messages:
                log.info("Maintenance removed events=%s outbox=%s", events, messages)
        except Exception as exc:  # noqa: BLE001
            log.exception("Maintenance cleanup failed: %s", exc)


def start_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_worker, name="postgres-maintenance", daemon=True).start()
        _started = True
        log.info("PostgreSQL maintenance worker started")
