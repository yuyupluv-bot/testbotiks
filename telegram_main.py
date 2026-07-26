"""Telegram passenger bot: shared taxi FSM, VK drivers, HTTPS webhook."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

import requests
from flask import Flask, jsonify, request

from common.config import config
from common.database import session_scope
from common.db_migrate import ensure_schema
from common.logger import get_logger
from common.models import TelegramProcessedEvent, User
from bot import telegram_messaging
from bot.handlers import handle_passenger, show_main_menu
from bot.states_service import States, get_state, reset

log = get_logger("telegram.main")
app = Flask(__name__)
STRICT_PHONE_RE = re.compile(r"^\+79\d{9}$")


def _manual_phone(text: str) -> str | None:
    value = (text or "").strip()
    if STRICT_PHONE_RE.fullmatch(value):
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("79"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("89"):
        return "+7" + digits[1:]
    return None


def _contact_phone(message: dict, telegram_user_id: int) -> str | None:
    contact = message.get("contact") if isinstance(message, dict) else None
    if not isinstance(contact, dict):
        return None
    # A forwarded or manually selected third-party contact is not accepted.
    try:
        if int(contact.get("user_id")) != int(telegram_user_id):
            return None
    except (TypeError, ValueError):
        return None
    digits = re.sub(r"\D", "", str(contact.get("phone_number") or ""))
    if len(digits) == 11 and digits.startswith("79"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("89"):
        return "+7" + digits[1:]
    return None


def _synthetic_vk_id(telegram_user_id: int) -> int:
    digest = hashlib.blake2b(f"telegram:{telegram_user_id}".encode(), digest_size=8).digest()
    return -(8_100_000_000_000_000_000 + int.from_bytes(digest, "big") % 100_000_000_000_000_000)


def _identity(update: dict) -> tuple[int | None, int | None, str, dict, dict]:
    callback = update.get("callback_query") if isinstance(update.get("callback_query"), dict) else {}
    message = update.get("message") if isinstance(update.get("message"), dict) else {}
    if callback:
        sender = callback.get("from") or {}
        callback_message = callback.get("message") or {}
        chat = callback_message.get("chat") or {}
        active_message = callback_message
    else:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        active_message = message
    try:
        user_id = int(sender.get("id"))
        chat_id = int(chat.get("id"))
    except (TypeError, ValueError):
        return None, None, "", active_message, callback
    name = " ".join(value for value in (sender.get("first_name"), sender.get("last_name")) if value)
    name = name or sender.get("username") or f"Telegram {user_id}"
    return user_id, chat_id, name, active_message, callback


def _load_user(session, telegram_user_id: int, telegram_chat_id: int, name: str) -> User:
    user = session.query(User).filter(User.telegram_user_id == telegram_user_id).one_or_none()
    if user is None:
        user = User(
            vk_id=_synthetic_vk_id(telegram_user_id),
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            full_name=name,
            role="passenger",
            granted_roles="passenger",
        )
        session.add(user)
        session.flush()
    else:
        user.telegram_chat_id = telegram_chat_id
        if name:
            user.full_name = name
    return user


def _ask_phone(user: User, invalid: bool = False) -> None:
    prefix = "❌ Нужен российский мобильный номер строго вида +79XXXXXXXXX.\n\n" if invalid else ""
    telegram_messaging.enqueue_direct(
        int(user.telegram_chat_id),
        prefix + "📱 Для заказа такси нажмите кнопку и поделитесь номером телефона.\n"
        "Принимается только ваш номер в формате +79XXXXXXXXX.",
        reply_markup={
            "keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
            "input_field_placeholder": "+79XXXXXXXXX",
        },
    )


def _payload(callback: dict) -> dict:
    raw = callback.get("data") if callback else None
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"cmd": str(raw)}


def _answer_callback(callback: dict) -> None:
    callback_id = callback.get("id") if callback else None
    if not callback_id:
        return
    try:
        requests.post(
            ("http" + "s://" + "api.telegram.org/bot" + config.TELEGRAM_BOT_TOKEN + "/answerCallbackQuery"),
            json={"callback_query_id": callback_id},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not answer Telegram callback: %s", exc)


def process_update(update: dict) -> None:
    update_id = update.get("update_id")
    try:
        update_id = int(update_id)
    except (TypeError, ValueError):
        return
    telegram_user_id, chat_id, name, message, callback = _identity(update)
    if not telegram_user_id or not chat_id:
        return
    text = str(message.get("text") or "").strip()

    with session_scope() as session:
        if session.query(TelegramProcessedEvent.id).filter(TelegramProcessedEvent.update_id == update_id).first():
            return
        session.add(TelegramProcessedEvent(update_id=update_id))
        session.flush()
        user = _load_user(session, telegram_user_id, chat_id, name)
        if user.is_blocked:
            return

        if not user.phone:
            phone = _contact_phone(message, telegram_user_id) or _manual_phone(text)
            if phone:
                user.phone = phone
                reset(session, user.vk_id, States.MAIN_MENU)
                telegram_messaging.enqueue_direct(
                    chat_id,
                    f"✅ Номер {phone} сохранён.",
                    reply_markup={"remove_keyboard": True},
                )
                show_main_menu(session, user)
            else:
                _ask_phone(user, invalid=bool(text or message.get("contact")))
            return

        if text.casefold() in ("/start", "start", "начать", "меню"):
            reset(session, user.vk_id, States.MAIN_MENU)
            show_main_menu(session, user)
            return

        state = get_state(session, user.vk_id).state
        handle_passenger(session, user, state, text, _payload(callback), [])
    _answer_callback(callback)


@app.get("/health")
def health():
    return jsonify(status="ok", service="telegram-taxi")


@app.get("/healthz")
def healthz():
    try:
        from sqlalchemy import text
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        return jsonify(status="ok", database="ok")
    except Exception as exc:  # noqa: BLE001
        return jsonify(status="error", error=str(exc)), 503


@app.post("/webhooks/telegram")
def telegram_webhook():
    if config.TELEGRAM_WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if supplied != config.TELEGRAM_WEBHOOK_SECRET:
            return jsonify(error="forbidden"), 403
    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return jsonify(error="invalid json"), 400
    try:
        process_update(update)
    except Exception as exc:  # noqa: BLE001
        log.exception("Telegram update failed: %s", exc)
        return jsonify(error="processing failed"), 500
    return jsonify(ok=True)


def subscribe() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    if not config.TELEGRAM_WEBHOOK_URL:
        raise RuntimeError("TELEGRAM_WEBHOOK_URL is empty")
    body = {
        "url": config.TELEGRAM_WEBHOOK_URL,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    if config.TELEGRAM_WEBHOOK_SECRET:
        body["secret_token"] = config.TELEGRAM_WEBHOOK_SECRET
    response = requests.post(
        ("http" + "s://" + "api.telegram.org/bot" + config.TELEGRAM_BOT_TOKEN + "/setWebhook"),
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result))
    print("TELEGRAM WEBHOOK SUBSCRIBED:", config.TELEGRAM_WEBHOOK_URL)


def run() -> None:
    if not ensure_schema():
        raise RuntimeError("Database migration failed")
    telegram_messaging.start_worker()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)


if __name__ == "__main__":
    if "--subscribe" in sys.argv:
        if not ensure_schema():
            raise SystemExit("Database migration failed")
        subscribe()
    else:
        run()
