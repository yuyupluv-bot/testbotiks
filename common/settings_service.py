"""Helpers to read/write the key/value `settings` table with sane defaults."""
from __future__ import annotations

from typing import Any
import ast
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

import threading
import time

from .config import config
from .models import Setting

# Short thread-safe cache; external admin changes appear within SETTINGS_CACHE_TTL.
_CACHE_TTL = float(config.SETTINGS_CACHE_TTL)
_cache: dict[str, tuple[float, str | None]] = {}
_cache_lock = threading.RLock()

DEFAULTS: dict[str, str] = {
    "welcome_message": "Добро пожаловать в наше такси! Выберите роль, чтобы продолжить.",
    "city_id_default": "1",
    "base_fare": str(config.BASE_FARE),
    "price_per_km": str(config.PRICE_PER_KM),
    "min_fare": str(config.MIN_FARE),
    "penalty_fine": str(config.PENALTY_FINE),
    "free_waiting_minutes": str(config.FREE_WAITING_MINUTES),
    "price_per_waiting_minute": str(config.PRICE_PER_WAITING_MINUTE),
    "driver_accept_timeout": str(config.DRIVER_ACCEPT_TIMEOUT),
    # --- requirement 3/7/8: passenger UI labels, price, support (admin-editable)
    "price_text": "\U0001F3F7 Тарифы: посадка 100 ₽, км — 20 ₽. Точную стоимость назовёт водитель.",
    "support_link": config.SUPPORT_LINK,
    "support_text": "Если нужна помощь, напишите в поддержку: {link}",
    "community_link": f"https://vk.com/club{config.VK_GROUP_ID}" if config.VK_GROUP_ID else "https://vk.com/",
    "community_rules": "Правила сообщества:\n1. Указывайте точные адреса.\n2. Уважайте водителей и пассажиров.\n3. Отменяйте неактуальные заявки своевременно.",
    "msg_subscription_required": "Вы должны быть подписаны на сообщество: {link}",
    "msg_subscription_still_required": "Вы ещё не подписаны на сообщество: {link}",
    "msg_subscription_check_error": "Не удалось проверить подписку. Убедитесь, что вы подписались на сообщество, и нажмите «Я подписался» ещё раз: {link}",
    "msg_fake_account": "🚫 Ваш аккаунт похож на фейковый.",
    "btn_check_subscription": "Я подписался",
    "btn_new_order": "\U0001F695 Заказать авто",
    "btn_booking": "📅 Забронировать поездку",
    "btn_rules": "📜 Правила",
    "btn_my_booking": "🗓 Моя бронь",
    "btn_drivers": "👥 Свободные водители",
    "btn_price": "\U0001F3F7 Прайс",
    "btn_price_calculate": "🧮 Примерный расчёт",
    "price_calc_per_km": "35",
    "btn_my_reviews": "\u2B50 Мои отзывы",
    "btn_support": "\U0001F198 Поддержка",
    "btn_price_back": "\u2B05\uFE0F Назад",
    # --- requirement 2/4: verification + passenger waiting queue
    "passenger_poll_timeout": str(config.PASSENGER_POLL_TIMEOUT),
    "verify_enabled": "1" if config.VERIFY_ENABLED else "0",
    "verify_min_account_months": str(config.VERIFY_MIN_ACCOUNT_MONTHS),
    # --- Extended features: extra services / night tariff / delivery / blocks (req.1-6) ---
    "svc_baggage_price": "50",
    "svc_animal_price": "50",
    "night_start_hour": "23",
    "night_end_hour": "6",
    "night_surcharge_amount": "50",
    "delivery_confirm_timeout": "180",
    "driver_cancel_grace_seconds": "120",
    "driver_violation_reset_days": "30",
    "driver_block_1_hours": "1",
    "driver_block_2_hours": "24",
    "driver_block_3_hours": "168",
    "passenger_cancel_grace_seconds": "120",
    "fake_call_fine_mode": "fixed",
    "fake_call_fine": "100",
    "fake_call_fine_percent": "50",
    "fake_call_reminder_hours": "2",
    "fake_call_reminder_max": "3",
    "btn_fake_calls": "🚫 Ложные вызовы",
    # --- Lines feature (req 1.1/1.2) editable message templates ---
    "msg_choose_line": "🧭 Выберите линию (город), на которой будете работать:",
    "msg_line_selected": "✅ Вы на линии «{line}». Теперь вы можете принимать заявки.",
    "msg_line_stay": "✅ Вы остались на линии «{line}».",
    "msg_line_left": "🚪 Вы вышли с линии. Заявки поступать не будут — выберите линию в меню.",
    "msg_post_ride_line": "Вы остаётесь на линии «{line}» или хотите сменить?",
    "msg_need_line": "🧭 Выберите линию в меню, чтобы принимать заказы.",
    "msg_pick_order_city": "🏙 В каком городе (на какой линии) заказываете?",
    "msg_choose_order_type": "Что оформляем?",
    "msg_no_drivers_on_line": "😔 На данной линии сейчас нет свободных водителей. Выберите другую линию или подождите.",
    # --- Global tariff fallbacks (per-line values live under tariff:<city_id>:*) ---
    "price_per_km": "30",
    "min_price": "100",
    # --- Editable message templates (single-brace .format placeholders) ---
    "msg_extras_prompt": "Выберите доп. услуги (можно несколько) и нажмите «Далее»:",
    "msg_order_confirm": "Ваша заявка: {request}. Проверьте адрес, отредактируйте в случае ошибки, подтвердите чтобы мы могли найти вам водителя",
    "msg_night_tariff_notice": "Действует ночной тариф (+{amount:.0f} ₽ к стоимости).",
    "msg_waiting_started": "Ожидание запущено. Первые {free} мин бесплатно, далее {rate:.0f} ₽/мин.",
    "msg_waiting_stopped": "Ожидание завершено: {minutes} мин, стоимость {cost:.0f} ₽.",
    "msg_delivery_ask_price": "Введите сумму, за которую вы готовы выполнить доставку (в рублях):",
    "msg_delivery_offer": "🚗 Водитель готов выполнить вашу доставку за {price:.0f} ₽ + оплата чека из магазина. Согласны?",
    "msg_delivery_search_next": "Водитель не устроил, ищем другого.",
    "msg_delivery_no_drivers": "Нет водителей для доставки по вашим условиям.",
    "msg_driver_blocked": "Вы отменили заказ после его принятия. Ваш аккаунт заблокирован до {until}.",
    "msg_fake_call_notice": "Вы отменили заказ после 2 минут. Это считается ложным вызовом. Вам нужно связаться с водителем для оплаты ложного вызова. Нажмите «Я готов оплатить».",
    "msg_driver_no_show_false_call": "Вы не вышли, вы должны оплатить ложный вызов.",
    "msg_fake_call_pay_info": "Свяжитесь с водителем для оплаты штрафа: {driver_mention}",
    "msg_fake_call_reminder": "Напоминание: свяжитесь с водителем для оплаты штрафа: {driver_mention}",
    "msg_fake_call_paid": "Спасибо за оплату ложного вызова, вы можете пользоваться ботом дальше, удачных поездок!",
    "msg_queue_first": "🥇 Вы первый в очереди! Следующая заявка ваша — ожидайте заявку.",
    "msg_fake_call_locked": (
        "\U0001F6AB Доступ к боту ограничен: за вами числится неоплаченный ложный вызов.\n"
        "Вам нужно связаться с водителем для оплаты ложного вызова.\n"
        "Пока он не оплачен, другие действия недоступны.\n"
        "Нажмите «Я готов оплатить», чтобы получить ссылку на страницу водителя для оплаты."
    ),
    # --- Requirement 1: driver arrival-time (ETA) menu -------------------- #
    # Comma-separated preset options (minutes) shown as buttons + custom entry.
    "eta_options": "5,10,15,20",
    "btn_eta_custom": "✏️ Индивидуальное время",
    "msg_eta_menu": "⏱ Через сколько вы будете у пассажира? Выберите вариант или укажите своё время:",
    "msg_eta_custom_prompt": "Введите время прибытия в минутах (число):",
    "msg_eta_passenger": "🚕 Водитель прибудет через {minutes} мин.",
    "msg_eta_saved": "Готово. Пассажиру отправлено: прибытие через {minutes} мин.",
    # --- Requirement 3: automatic line detection (no passenger choice) ---- #
    "default_line": "Пашия",
    # --- Requirement 4: main menu locked while an order is active --------- #
    "msg_menu_locked": "🚫 Вы не можете перейти в меню, пока активна заявка. Дождитесь её завершения.",
    # --- Requirement 9: text shown when all drivers are busy -------------- #
    "msg_wait_first_free": "⏳ Дождитесь, первый свободный водитель возьмёт заявку.",
    "msg_no_free_drivers_chat": "Кто на линию, есть заявки, которые ждут свободного на линии.",
    # --- Requirement 5: «Моя машина» edit confirmation -------------------- #
    "msg_car_edit_confirm": "🚗 Данные вашего автомобиля:\n{car}\n\nВы хотите изменить данные о машине?",
    "btn_car_edit": "✏️ Отредактировать авто",
    "btn_car_back": "⬅️ Вернуться в главное меню",
    "msg_car_ask_model": "Введите марку автомобиля (напр. Kia Rio):",
    "msg_car_ask_color": "Введите цвет автомобиля:",
    "msg_car_ask_number": "Введите госномер автомобиля:",
    # --- Requirement 6: order flow (addresses in one message + edit) ------ #
    "msg_ask_addresses": "📍 Откуда и куда едем? Напишите адрес подачи и назначения в одном сообщении.",
    "msg_order_text_prompt": "🚕 Напишите текст заявки одним сообщением: откуда и куда ехать, адреса и важные детали.",
    "msg_addresses_parse_error": "Не удалось обработать маршрут. Напишите адрес подачи и назначения одним непустым сообщением.",
    "msg_freight_contact_dispatcher": "По поводу грузоперевозок обращайтесь к диспетчеру с 7:00 до 21:00.",
    "btn_edit_order": "✏️ Отредактировать заявку",
    # --- Requirement 2: free drivers per line (driver «Сменить линию») ---- #
    "msg_lines_overview_header": "🧭 Линии и свободные водители:",
    "msg_line_no_free": "нет свободных",
    # --- Requirement 8: driver accept timeout (moved to tail, kept on line)
    "driver_accept_timeout": "90",
    "driver_chat_timeout": str(config.DRIVER_CHAT_TIMEOUT),
    "driver_chat_far_timeout": "10800",
    "driver_chat_delivery_timeout": "3600",
    "booking_chat_timeout": "21600",
    "driver_chat_peer_id": str(config.DRIVER_CHAT_PEER_ID),
    "driver_fallback_chat_peer_id": str(config.DRIVER_FALLBACK_CHAT_PEER_ID),
    "msg_offer_timeout_driver": "⏰ Вы не приняли заявку за 90 секунд. Вы перемещены в конец очереди, но остаётесь на линии.",
    "msg_contact_dispatcher": "☎️ Отменить заявку может только диспетчер. Напишите диспетчеру: {link}",
    "dispatcher_contact_link": "https://vk.com/im",
    "msg_waiting_passenger_started": "⏳ Водитель ожидает вас. Первые {free} минуты бесплатно, далее {rate:.0f} руб/минута.",
    "msg_waiting_passenger_continued": "▶️ Поездка продолжена. Спасибо за ожидание!",
}


@lru_cache(maxsize=1)
def active_text_keys() -> tuple[str, ...]:
    """Return only text settings referenced by executable bot callsites."""
    candidates = {
        key for key in DEFAULTS
        if key.startswith("msg_") or key in {
            "welcome_message", "price_text", "support_text",
            "community_rules", "community_link", "support_link",
        }
    }
    used: set[str] = set()
    root = Path(__file__).resolve().parents[1] / "bot"
    for path in root.glob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            )
            key_node = node.args[1]
            if name in {"msg", "get_setting"} and isinstance(key_node, ast.Constant):
                if key_node.value in candidates:
                    used.add(key_node.value)
    # These values are read through small helper functions rather than direct
    # callsites, but are all visible to passengers.
    used.update({
        "welcome_message", "price_text", "support_text",
        "community_rules", "community_link", "support_link",
    } & candidates)
    return tuple(sorted(used))


def get_setting(session: Session, key: str, default: Any | None = None) -> str | None:
    """Read a setting through a short thread-safe cache.

    Hot message paths previously queried PostgreSQL dozens of times per event.
    A 30-second cache keeps admin changes responsive while removing most of
    that database load. Local writes invalidate immediately.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            value = hit[1]
            return value if value is not None else (str(default) if default is not None else DEFAULTS.get(key))
    row = session.query(Setting).filter(Setting.key == key).one_or_none()
    value = row.value if row is not None and row.value is not None else None
    with _cache_lock:
        _cache[key] = (now, value)
    if value is not None:
        return value
    if default is not None:
        return str(default)
    return DEFAULTS.get(key)


def get_float(session: Session, key: str, default: float = 0.0) -> float:
    value = get_setting(session, key)
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def get_int(session: Session, key: str, default: int = 0) -> int:
    return int(get_float(session, key, default))


def set_setting(session: Session, key: str, value: Any) -> None:
    row = session.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value=str(value))
        session.add(row)
    else:
        row.value = str(value)
    with _cache_lock:
        _cache.pop(key, None)  # local changes are picked up at once


def ensure_defaults(session: Session) -> None:
    existing = {key for (key,) in session.query(Setting.key).filter(Setting.key.in_(DEFAULTS)).all()}
    session.add_all(Setting(key=key, value=value) for key, value in DEFAULTS.items() if key not in existing)


def get_all_settings(session: Session) -> dict[str, str]:
    """Load all admin settings in one query instead of one query per field."""
    rows = session.query(Setting.key, Setting.value).filter(Setting.key.in_(DEFAULTS)).all()
    stored = {key: value for key, value in rows}
    return {key: stored.get(key, default) for key, default in DEFAULTS.items()}


def get_bool(session: Session, key: str, default: bool = False) -> bool:
    value = get_setting(session, key)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


def get_cached(session: Session, key: str, default: Any | None = None) -> str | None:
    return get_setting(session, key, default)


def invalidate(key: str | None = None) -> None:
    with _cache_lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


def button_label(session: Session, key: str, default: str) -> str:
    value = get_cached(session, key, default)
    return value or default


def msg(session: Session, key: str, **fmt: object) -> str:
    """Return an admin-editable message template, formatted with **fmt.

    Templates live in the settings table (key must start with "msg_"). Falls
    back to DEFAULTS, and if formatting fails returns the raw template so a
    bad placeholder never crashes the bot (requirement 8).
    """
    template = get_cached(session, key, DEFAULTS.get(key, "")) or DEFAULTS.get(key, "")
    if not template:
        return ""
    if fmt:
        try:
            return template.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return template
    return template
