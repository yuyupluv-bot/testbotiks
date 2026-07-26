"""Transactional Telegram delivery and VK-keyboard conversion."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

import requests

from common.config import config
from common.database import SessionLocal, current_session
from common.logger import get_logger
from common.models import TelegramOutboxMessage, User

log = get_logger("bot.telegram")
_started = False
_start_lock = threading.Lock()


def vk_keyboard_to_telegram(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        source = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    rows = []
    for row in source.get("buttons") or []:
        converted = []
        for button in row:
            action = button.get("action") or {}
            label = str(action.get("label") or "Кнопка")
            if action.get("type") == "open_link":
                converted.append({"text": label, "url": action.get("link", "")})
                continue
            payload = action.get("payload") or "{}"
            if not isinstance(payload, str):
                payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            else:
                try:
                    payload = json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            if len(payload.encode("utf-8")) <= 64:
                converted.append({"text": label, "callback_data": payload})
            else:
                log.error("Telegram callback_data exceeds 64 bytes: %s", payload)
        if converted:
            rows.append(converted)
    return {"inline_keyboard": rows} if rows else None


def enqueue_direct(chat_id: int, text: str, reply_markup: dict | None = None) -> bool:
    db = current_session()
    if db is None:
        return False
    db.add(TelegramOutboxMessage(
        chat_id=chat_id,
        text=(text or "")[:4096],
        reply_markup=json.dumps(reply_markup, ensure_ascii=False) if reply_markup else None,
        status="pending",
        next_attempt_at=dt.datetime.now(dt.timezone.utc),
    ))
    return True


def enqueue_for_synthetic_vk_id(peer_id: int, text: str = "", keyboard: str | None = None, attachment=None) -> bool:
    db = current_session()
    if db is None:
        return False
    user = db.query(User).filter(User.vk_id == peer_id, User.telegram_chat_id.isnot(None)).one_or_none()
    if not user:
        return False
    reply_markup = vk_keyboard_to_telegram(keyboard)
    now = dt.datetime.now(dt.timezone.utc)
    markup_json = json.dumps(reply_markup, ensure_ascii=False) if reply_markup else None
    duplicate = db.query(TelegramOutboxMessage.id).filter(
        TelegramOutboxMessage.chat_id == user.telegram_chat_id,
        TelegramOutboxMessage.status.in_(("pending", "sending", "failed")),
        TelegramOutboxMessage.text == (text or "")[:4096],
        TelegramOutboxMessage.reply_markup == markup_json,
        TelegramOutboxMessage.created_at >= now - dt.timedelta(seconds=5),
    ).first()
    if duplicate:
        return True
    db.add(TelegramOutboxMessage(
        chat_id=user.telegram_chat_id,
        text=(text or "")[:4096],
        reply_markup=markup_json,
        status="pending",
        next_attempt_at=now,
    ))
    return True


def _claim() -> list[int]:
    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal.begin() as db:
        rows = db.query(TelegramOutboxMessage).filter(
            TelegramOutboxMessage.status.in_(("pending", "failed")),
            TelegramOutboxMessage.next_attempt_at <= now,
        ).order_by(TelegramOutboxMessage.id).with_for_update(skip_locked=True).limit(20).all()
        ids = []
        for row in rows:
            row.status = "sending"
            row.claimed_at = now
            ids.append(row.id)
        return ids


def _send(row_id: int) -> None:
    with SessionLocal.begin() as db:
        row = db.get(TelegramOutboxMessage, row_id)
        if not row or row.status != "sending":
            return
        body = {"chat_id": row.chat_id, "text": row.text or ""}
        if row.reply_markup:
            try:
                body["reply_markup"] = json.loads(row.reply_markup)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        try:
            response = requests.post(
                ("http" + "s://" + "api.telegram.org/bot" + config.TELEGRAM_BOT_TOKEN + "/sendMessage"),
                json=body,
                timeout=20,
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(str(result))
            row.status = "sent"
            row.sent_at = dt.datetime.now(dt.timezone.utc)
            row.last_error = None
        except Exception as exc:  # noqa: BLE001
            row.attempts += 1
            row.last_error = str(exc)[:2000]
            if row.attempts >= 10:
                row.status = "cancelled"
            else:
                row.status = "failed"
                row.next_attempt_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=min(300, 2 ** row.attempts))


def _worker() -> None:
    while True:
        try:
            ids = _claim()
            if not ids:
                time.sleep(0.5)
                continue
            for row_id in ids:
                _send(row_id)
                time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            log.exception("Telegram outbox worker failed: %s", exc)
            time.sleep(2)


def start_worker() -> None:
    global _started
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    with _start_lock:
        if _started:
            return
        for index in range(config.TELEGRAM_OUTBOX_WORKERS):
            threading.Thread(target=_worker, name=f"telegram-outbox-{index}", daemon=True).start()
        _started = True
