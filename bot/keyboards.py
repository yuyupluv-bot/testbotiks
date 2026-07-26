"""VK keyboard builders (returns JSON strings ready for messages.send).

NOTE: this module was rebuilt to stay consistent with handlers/services.
It exposes every builder referenced across the bot: passenger extras & order
confirmation, delivery confirmation, paid-waiting toggle, false-calls, driver
line selection («lines» feature) and the post-ride line question.
"""
from __future__ import annotations

import json
import math
from typing import Any

from .roles import ROLE_TITLES

GREEN = "positive"
RED = "negative"
BLUE = "primary"
WHITE = "secondary"

# VK regular keyboards allow ten rows with up to four buttons per row; inline
# keyboards allow six rows. Error 911 "button [x][0] has invalid action" was
# not a row-limit error: the normalizer accidentally wrapped every row in one
# extra list, turning the button action into an array instead of an object.
VK_REGULAR_MAX_ROWS = 10
VK_INLINE_MAX_ROWS = 6
VK_REGULAR_MAX_BUTTONS_PER_ROW = 4
VK_INLINE_MAX_BUTTONS_PER_ROW = 5


def _link_btn(label: str, link: str) -> dict:
    return {"action": {"type": "open_link", "label": label, "link": link}}


def _btn(label: str, color: str = WHITE, payload: dict | None = None) -> dict:
    action: dict[str, Any] = {"type": "text", "label": label}
    if payload is not None:
        action["payload"] = json.dumps(payload, ensure_ascii=False)
    return {"action": action, "color": color}


def keyboard(rows: list[list[dict]], one_time: bool = False, inline: bool = False) -> str:
    max_rows = VK_INLINE_MAX_ROWS if inline else VK_REGULAR_MAX_ROWS
    max_per_row = (
        VK_INLINE_MAX_BUTTONS_PER_ROW
        if inline
        else VK_REGULAR_MAX_BUTTONS_PER_ROW
    )

    # Most bot menus intentionally keep one large button per row.  If a
    # dynamic list grows beyond VK's row limit, compact all existing buttons
    # without dropping actions.  Existing layouts below the limit remain
    # unchanged.
    normalized: list[list[dict]] = []
    for row in rows:
        if not row:
            continue
        normalized.extend(
            row[index:index + max_per_row]
            for index in range(0, len(row), max_per_row)
        )

    if len(normalized) > max_rows:
        buttons = [button for row in normalized for button in row]
        if len(buttons) > max_rows * max_per_row:
            raise ValueError(
                "VK keyboard contains too many buttons: "
                f"{len(buttons)} > {max_rows * max_per_row}"
            )
        width = min(max_per_row, max(1, math.ceil(len(buttons) / max_rows)))
        normalized = [
            buttons[index:index + width]
            for index in range(0, len(buttons), width)
        ]

    # Fail locally instead of enqueueing malformed JSON that VK rejects with
    # error 911. Every cell must be a button object with an action object.
    for row_index, row in enumerate(normalized):
        for button_index, button in enumerate(row):
            if not isinstance(button, dict) or not isinstance(button.get("action"), dict):
                raise ValueError(
                    "Invalid VK keyboard button at "
                    f"[{row_index}][{button_index}]: action must be an object"
                )

    return json.dumps(
        {"one_time": one_time, "inline": inline, "buttons": normalized},
        ensure_ascii=False,
    )


def empty() -> str:
    return json.dumps({"buttons": [], "one_time": True})


def _role_switch_row() -> list[dict]:
    return [_btn("🔁 Сменить роль", WHITE, {"cmd": "change_role"})]


def role_choice_keyboard(roles: list[str]) -> str:
    rows = [
        [_btn(ROLE_TITLES.get(r, r), BLUE, {"cmd": "role_set", "role": r})]
        for r in roles
    ]
    rows.append([_btn("⬅️ Назад", WHITE, {"cmd": "start"})])
    return keyboard(rows, one_time=True)


# --------------------------------------------------------------------------- #
#  Passenger                                                                   #
# --------------------------------------------------------------------------- #
def passenger_menu(
    show_role_switch: bool = False,
    labels: dict | None = None,
    has_booking: bool = False,
) -> str:
    """Main passenger menu. Bug #2: «Заказать авто» (was «Заказать такси»)."""
    labels = labels or {}
    rows = [
        [_btn(labels.get("btn_new_order", "🚕 Заказать авто"), GREEN, {"cmd": "new_order"})],
        [_btn(labels.get("btn_drivers", "👥 Свободные водители"), BLUE, {"cmd": "drivers"})],
    ]
    # Booking always occupies its own full-width row. When a passenger has an
    # active booking, that same row changes to «Моя бронь».
    if has_booking:
        booking_button = _btn(
            labels.get("btn_my_booking", "🗓 Моя бронь"),
            GREEN,
            {"cmd": "my_booking"},
        )
    else:
        booking_button = _btn(
            labels.get("btn_booking", "📅 Забронировать поездку"),
            BLUE,
            {"cmd": "booking_start"},
        )
    rows.append([booking_button])
    rows.append([
        _btn(labels.get("btn_price", "🏷 Прайс"), WHITE, {"cmd": "price"}),
        _btn(labels.get("btn_rules", "📜 Правила"), WHITE, {"cmd": "rules"}),
    ])
    if show_role_switch:
        rows.append(_role_switch_row())
    return keyboard(rows)


def price_menu_keyboard(
    children: list[tuple[str, str]],
    labels: dict | None = None,
    active_order: bool = False,
) -> str:
    """Build the public price menu with two section buttons per row.

    The approximate calculator is intentionally hidden while a driver has an
    active order. The final full-width row always returns to the correct menu.
    """
    labels = labels or {}
    rows: list[list[dict]] = []
    for index in range(0, len(children), 2):
        rows.append([
            _btn(title, WHITE, {"cmd": "price_section", "key": key})
            for key, title in children[index:index + 2]
        ])
    if not active_order:
        rows.append([
            _btn(
                labels.get("btn_price_calculate", "🧮 Примерный расчёт"),
                BLUE,
                {"cmd": "price_calculate"},
            )
        ])
    back_label = (
        "⬅️ Вернуться к активной заявке"
        if active_order
        else "⬅️ Вернуться в главное меню"
    )
    rows.append([_btn(back_label, WHITE, {"cmd": "price_back"})])
    return keyboard(rows)


def order_type_keyboard() -> str:
    """Req 1.3: shown right after «Заказать авто»."""
    return keyboard(
        [
            [_btn("🚕 Обычный заказ", GREEN, {"cmd": "set_order_type", "type": "regular"})],
            [_btn("📦 Доставка", BLUE, {"cmd": "set_order_type", "type": "delivery"})],
            [_btn("❌ Отмена", RED, {"cmd": "cancel_order"})],
        ]
    )


def cities_keyboard(cities: list[tuple[int, str]]) -> str:
    """Passenger picks the line (city) for the order."""
    rows = [[_btn(name, WHITE, {"cmd": "pick_city", "city_id": cid})] for cid, name in cities]
    rows.append([_btn("❌ Отмена", RED, {"cmd": "cancel_order"})])
    return keyboard(rows, one_time=True)


def extras_keyboard(selection) -> str:
    """Req 1 (existing): toggle extra services, then «Далее»."""
    from . import extra_services

    def _is_on(sel, key: str) -> bool:
        if isinstance(sel, dict):
            return bool(sel.get(key))
        if isinstance(sel, (list, tuple, set)):
            return key in sel
        return False

    rows = []
    for svc in extra_services.SERVICES:
        mark = "✅ " if _is_on(selection, svc["key"]) else "◻️ "
        rows.append([_btn(mark + svc["label"], WHITE, {"cmd": "toggle_service", "service": svc["key"]})])
    rows.append([_btn("➡️ Далее", GREEN, {"cmd": "extras_done"})])
    rows.append([_btn("❌ Отмена", RED, {"cmd": "cancel_order"})])
    return keyboard(rows)


def order_confirm_keyboard(edit_label: str = "✏️ Отредактировать заявку") -> str:
    """Requirement 6: adds an «Отредактировать заявку» button so the
    passenger can re-enter addresses / services before final confirmation."""
    return keyboard(
        [
            [_btn("✅ Подтвердить заказ", GREEN, {"cmd": "confirm_order"})],
            [_btn(edit_label, BLUE, {"cmd": "edit_order"})],
            [_btn("❌ Отмена", RED, {"cmd": "cancel_order"})],
        ]
    )


def delivery_confirm_keyboard(order_id: int) -> str:
    """Req 4 / bug #5: passenger agrees or declines the driver's delivery price."""
    return keyboard(
        [
            [_btn("✅ Согласен", GREEN, {"cmd": "delivery_agree", "order_id": order_id})],
            [_btn("❌ Отказаться", RED, {"cmd": "delivery_decline", "order_id": order_id})],
        ],
        inline=True,
    )


def delivery_comment_keyboard() -> str:
    """Delivery comment step: let the passenger skip the comment entirely."""
    return keyboard(
        [
            [_btn("⏭ Пропустить комментарий", WHITE, {"cmd": "skip_comment"})],
            [_btn("❌ Отмена", RED, {"cmd": "cancel_order"})],
        ]
    )


def passenger_waiting_keyboard() -> str:
    return keyboard([
        [_btn("📍 Статус заявки", WHITE, {"cmd": "order_status"})],
        [_btn("🚗 Водители", BLUE, {"cmd": "drivers"})],
        [_btn("❌ Отменить заявку", RED, {"cmd": "cancel_order"})],
    ])


def passenger_assigned_keyboard() -> str:
    """Driver is assigned but has not provided arrival time yet."""
    return keyboard([
        [_btn("🔗 Связаться с водителем", BLUE, {"cmd": "chat"})],
        [_btn("❌ Отменить поездку", RED, {"cmd": "cancel_ride"})],
    ])


def passenger_order_entry_keyboard() -> str:
    """Shown before an order exists; no premature status button."""
    return keyboard([
        [_btn("🚗 Водители", BLUE, {"cmd": "drivers"})],
        [_btn("❌ Отменить ввод заявки", RED, {"cmd": "cancel_flow"})],
    ])


def passenger_wait_choice_keyboard() -> str:
    return keyboard([
        [_btn("Да буду ждать", GREEN, {"cmd": "wait_join_yes"})],
        [_btn("Нет, отменить заявку", RED, {"cmd": "wait_join_no"})],
    ])

def passenger_after_cancel_keyboard() -> str:
    return keyboard([[_btn("🚕 Новая заявка", GREEN, {"cmd":"new_order"})]])


def passenger_repoll_keyboard(order_id: int) -> str:
    return keyboard(
        [
            [_btn("✅ Да", GREEN, {"cmd": "queue_yes", "order_id": order_id})],
            [_btn("❌ Нет", RED, {"cmd": "queue_no", "order_id": order_id})],
        ],
        inline=True,
    )



def subscription_keyboard(check_label: str) -> str:
    """Persistent onboarding menu; the community link is sent in the text."""
    return keyboard([[_btn(check_label, BLUE, {"cmd": "check_subscription"})]])


def passenger_departure_keyboard() -> str:
    return keyboard(
        [
            [_btn("✅ Да, жду", GREEN, {"cmd": "departure_wait"})],
            [_btn("❌ Нет, отменяю заявку", RED, {"cmd": "departure_cancel"})],
        ],
        inline=True,
    )



def chat_order_actual_keyboard(order_id: int) -> str:
    return keyboard(
        [
            [_btn("Да, актуальна", GREEN, {"cmd": "chat_order_actual_yes", "order_id": order_id})],
            [_btn("Нет, отменить", RED, {"cmd": "chat_order_actual_no", "order_id": order_id})],
        ],
        inline=True,
    )


def driver_depart_keyboard(order_id: int) -> str:
    return keyboard(
        [[_btn("🚕 Выезжаю", GREEN, {"cmd": "chat_depart", "order_id": order_id})]],
        inline=True,
    )


def review_comment_keyboard() -> str:
    return keyboard(
        [
            [_btn("Написать комментарий", BLUE, {"cmd": "review_comment_add"})],
            [_btn("Пропустить", WHITE, {"cmd": "review_comment_skip"})],
        ],
        inline=True,
    )


def passenger_ride_keyboard() -> str:
    return keyboard(
        [
            [_btn("⏱ Сколько ждать водителя?", WHITE, {"cmd": "driver_wait_remaining"})],
            [_btn("🔗 Связаться с водителем", BLUE, {"cmd": "chat"})],
            [_btn("❌ Отменить поездку", RED, {"cmd": "cancel_ride"})],
        ]
    )


def passenger_in_ride_keyboard() -> str:
    """Passenger menu after boarding: the ride can no longer be cancelled."""
    return keyboard([
        [_btn("🔗 Связь с водителем", BLUE, {"cmd": "chat"})],
    ])


def passenger_cancel_confirm_keyboard() -> str:
    return keyboard([
        [_btn("Да, отменить", RED, {"cmd": "cancel_confirm_yes"})],
        [_btn("Нет, продолжить поездку", GREEN, {"cmd": "cancel_confirm_no"})],
    ], inline=True)


def passenger_arrived_keyboard() -> str:
    return keyboard(
        [
            [_btn("🚶 Выхожу", GREEN, {"cmd": "going_out"})],
            [_btn("⏳ Подождать", WHITE, {"cmd": "wait_more"})],
            [_btn("🔗 Связаться с водителем", BLUE, {"cmd": "chat"})],
            [_btn("❌ Отменить заявку", RED, {"cmd": "cancel_order"})],
        ]
    )


def driver_delivery_keyboard(stage: str) -> str:
    rows = []
    if stage == "shopping":
        rows.append([_btn("🛒 Купил в магазине", GREEN, {"cmd": "bought"})])
    elif stage == "bought":
        rows.append([_btn("🏁 Завершить доставку", GREEN, {"cmd": "finish"})])
    rows.append([_btn("🔀 Параллельные заявки", WHITE, {"cmd": "parallel_orders"})])
    rows.append([_btn("🏷 Прайс", WHITE, {"cmd": "price"})])
    rows.append([_btn("❌ Отменить активную заявку", RED, {"cmd":"driver_cancel_active"})])
    return keyboard(rows)


def fake_call_pay_keyboard() -> str:
    return keyboard([[_btn("💳 Я готов оплатить", GREEN, {"cmd": "fake_pay"})]])


def rating_keyboard(order_id: int) -> str:
    rows = [[_btn("⭐" * i, WHITE, {"cmd": "rate", "order_id": order_id, "stars": i})]
            for i in range(5, 0, -1)]
    rows.append([_btn("Пропустить", WHITE, {"cmd": "skip_rate"})])
    return keyboard(rows, inline=True)


def passenger_rating_keyboard(order_id: int) -> str:
    """Requirement 3: driver rates the passenger (1-5) after the ride."""
    rows = [[_btn("⭐" * i, WHITE, {"cmd": "rate_passenger", "order_id": order_id, "stars": i})]
            for i in range(5, 0, -1)]
    rows.append([_btn("Пропустить", WHITE, {"cmd": "skip_rate_passenger"})])
    return keyboard(rows, inline=True)


def skip_keyboard() -> str:
    return keyboard([[_btn("Пропустить", WHITE, {"cmd": "skip"})]], one_time=True)


# --------------------------------------------------------------------------- #
#  Driver                                                                      #
# --------------------------------------------------------------------------- #
def driver_menu(
    on_line: bool,
    show_role_switch: bool = False,
    has_taken_bookings: bool = False,
) -> str:
    if on_line:
        rows = [
            [_btn("📋 Очередь", WHITE, {"cmd": "queue"})],
            [_btn("🧭 Сменить линию", BLUE, {"cmd": "choose_line"})],
            [_btn("⚙ Настройки", WHITE, {"cmd": "driver_settings"})],
            [
                _btn("🚫 Ложные вызовы", WHITE, {"cmd": "fake_calls"}),
                _btn("🏷 Прайс", WHITE, {"cmd": "price"}),
            ],
        ]
        if has_taken_bookings:
            rows.append([_btn("🗓 Мои брони", GREEN, {"cmd": "bookings_taken"})])
        rows.append([
            _btn("🛡 Уйти с линии", RED, {"cmd": "driver_offline"}),
            _btn("☕ Отлучиться", WHITE, {"cmd": "driver_away"}),
        ])
        rows.append([
            _btn("💰 Мои доходы", WHITE, {"cmd": "earnings"}),
            _btn("⭐ Мои отзывы", WHITE, {"cmd": "reviews"}),
        ])
    else:
        rows = [
            [
                _btn("✅ Выбрать линию", GREEN, {"cmd": "choose_line"}),
                _btn("👀 Кто на линии", BLUE, {"cmd": "who_online"}),
            ],
            [
                _btn("📋 Мои сведения", WHITE, {"cmd": "driver_info"}),
                _btn("📊 Статистика", WHITE, {"cmd": "driver_statistics"}),
            ],
        ]
        if has_taken_bookings:
            rows.append([_btn("🗓 Мои брони", GREEN, {"cmd": "bookings_taken"})])
        rows.append([
            _btn("🏷 Прайс", WHITE, {"cmd": "price"}),
            _btn("⏳ Ожидающие", BLUE, {"cmd": "pending_orders"}),
        ])
        if show_role_switch:
            rows.append([
                _btn("⚙ Настройки", WHITE, {"cmd": "driver_settings"}),
                _btn("🔁 Сменить роль", WHITE, {"cmd": "change_role"}),
            ])
        else:
            rows.append([_btn("⚙ Настройки", WHITE, {"cmd": "driver_settings"})])
    return keyboard(rows)


def driver_info_menu() -> str:
    """Personal driver sections collected outside the initial menu."""
    return keyboard([
        [_btn("🚫 Ложные вызовы", WHITE, {"cmd": "fake_calls"})],
        [_btn("💰 Мои доходы", WHITE, {"cmd": "earnings"})],
        [_btn("⭐ Мои отзывы", WHITE, {"cmd": "reviews"})],
        [_btn("⬅️ Вернуться в главное меню", WHITE, {"cmd": "start"})],
    ])


def missed_offer_timeout_keyboard() -> str:
    """Single safe exit after a driver is removed from the line by timeout."""
    return keyboard([
        [_btn("⬅️ Вернуться в главное меню", WHITE, {"cmd": "start"})],
    ])


def driver_away_menu(
    show_role_switch: bool = False,
    has_taken_bookings: bool = False,
) -> str:
    rows = [
        [_btn("▶️ Вернуться на линию", GREEN, {"cmd": "driver_online"})],
        [
            _btn("📋 Очередь", WHITE, {"cmd": "queue"}),
            _btn("⏳ Ожидающие", BLUE, {"cmd": "pending_orders"}),
        ],
        [
            _btn("⭐ Мои отзывы", WHITE, {"cmd": "reviews"}),
            _btn("🏷 Прайс", WHITE, {"cmd": "price"}),
        ],
        [_btn("⚙ Настройки", WHITE, {"cmd": "driver_settings"})],
        [_btn("🛡 Уйти с линии", RED, {"cmd": "driver_offline"})],
    ]
    if has_taken_bookings:
        rows.insert(3, [_btn("🗓 Мои брони", GREEN, {"cmd": "bookings_taken"})])
    if show_role_switch:
        rows.append(_role_switch_row())
    return keyboard(rows)


def booking_only_cancel_keyboard() -> str:
    """While filling out a booking, the passenger's persistent menu collapses
    to this single button. Tapping it returns them to the main menu."""
    return keyboard([
        [_btn("Не бронировать", WHITE, {"cmd": "booking_back"})],
    ])


def booking_comment_keyboard() -> str:
    return keyboard([
        [_btn("Пропустить комментарий", BLUE, {"cmd": "booking_comment_skip"})],
        [_btn("Не бронировать", WHITE, {"cmd": "booking_back"})],
    ])


def booking_rules_keyboard() -> str:
    return keyboard([
        [_btn("Заполнить бронь", GREEN, {"cmd": "booking_fill"})],
        [_btn("Не бронировать", WHITE, {"cmd": "booking_back"})],
    ])


def booking_type_keyboard() -> str:
    return keyboard([
        [_btn("Дальнее расстояние", BLUE, {"cmd": "booking_type", "type": "far_distance"})],
        [_btn("Определённое время", GREEN, {"cmd": "booking_type", "type": "early_time"})],
        [_btn("Не бронировать", WHITE, {"cmd": "booking_back"})],
    ])


def booking_date_keyboard() -> str:
    return keyboard([
        [_btn("На следующий день", GREEN, {"cmd": "booking_date_quick", "days": 1})],
        [_btn("Через 2 дня", BLUE, {"cmd": "booking_date_quick", "days": 2})],
        [_btn("Через 3 дня", BLUE, {"cmd": "booking_date_quick", "days": 3})],
        [_btn("Ввести свою дату", WHITE, {"cmd": "booking_date_custom"})],
        [_btn("Не бронировать", WHITE, {"cmd": "booking_back"})],
    ])


def booking_confirm_keyboard() -> str:
    return keyboard([
        [_btn("Подтвердить", GREEN, {"cmd": "booking_confirm"})],
        [_btn("Не бронировать", WHITE, {"cmd": "booking_back"})],
    ])


def my_booking_keyboard(booking_id: int) -> str:
    return keyboard([
        [_btn("Отменить бронь", RED, {"cmd": "booking_cancel", "booking_id": booking_id})],
        [_btn("Вернуться в главное меню", WHITE, {"cmd": "booking_back"})],
    ], inline=True)


def booking_take_keyboard(booking_id: int) -> str:
    return keyboard([
        [_btn(f"Я возьму бронь №{booking_id}", GREEN, {"cmd": "booking_take", "booking_id": booking_id})],
        [_btn("Никто не захотел взять бронь", RED, {"cmd": "booking_no_driver", "booking_id": booking_id})],
    ], inline=True)


def dispatcher_reply_keyboard(order_id: int) -> str:
    return keyboard([
        [_btn(
            f"Ответить на вопрос по заявке #{order_id}",
            BLUE,
            {"cmd": "disp_reply", "order_id": order_id},
        )],
    ], inline=True)


def booking_depart_keyboard(booking_id: int) -> str:
    return keyboard([
        [_btn("Я выезжаю", GREEN, {"cmd": "booking_depart", "booking_id": booking_id})],
        [_btn("Отменить бронь", RED, {"cmd": "booking_driver_cancel", "booking_id": booking_id})],
        [_btn("Вернуться в главное меню", WHITE, {"cmd": "booking_driver_back"})],
    ], inline=True)


def eta_keyboard(options: list[int], custom_label: str = "✏️ Индивидуальное время") -> str:
    """Requirement 1: preset arrival-time options + custom entry, shown to the
    driver right after they accept an order."""
    rows: list[list[dict]] = []
    row: list[dict] = []
    for minutes in options:
        row.append(_btn(f"{minutes} \u043c\u0438\u043d", BLUE, {"cmd": "eta_pick", "minutes": minutes}))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn(custom_label, WHITE, {"cmd": "eta_custom"})])
    return keyboard(rows, inline=True)


def delivery_eta_keyboard() -> str:
    """Delivery completion-time choices shown after passenger confirmation."""
    return eta_keyboard([10, 15, 20, 25], "✏️ Индивидуальное время")


def eta_add_keyboard() -> str:
    """Add delay to an ETA that has already been reported."""
    return keyboard(
        [
            [
                _btn("+3 мин", BLUE, {"cmd": "eta_add", "minutes": 3}),
                _btn("+5 мин", BLUE, {"cmd": "eta_add", "minutes": 5}),
                _btn("+7 мин", BLUE, {"cmd": "eta_add", "minutes": 7}),
            ],
            [_btn("✏️ Индивидуальное время", WHITE, {"cmd": "eta_add_custom"})],
        ],
        inline=True,
    )


def car_edit_keyboard(
    edit_label: str = "✏️ Отредактировать авто",
    back_label: str = "⬅️ Вернуться в главное меню",
) -> str:
    """Requirement 5: confirm before editing existing car data."""
    return keyboard(
        [
            [_btn(edit_label, GREEN, {"cmd": "car_edit"})],
            [_btn(back_label, WHITE, {"cmd": "car_back"})],
        ]
    )


def lines_keyboard(lines: list[tuple[int, str]], cmd: str = "set_line") -> str:
    """Req 1.1: choose one of the available lines."""
    rows = [[_btn(name, BLUE, {"cmd": cmd, "city_id": cid})] for cid, name in lines]
    rows.append([_btn("⬅️ Назад", WHITE, {"cmd": "start"})])
    return keyboard(rows)


def post_ride_line_keyboard(current_name: str) -> str:
    """Req 1.1: after finishing a ride — stay / change / leave the line."""
    return keyboard(
        [
            [_btn(f"✅ Остаться на «{current_name}»", GREEN, {"cmd": "stay_line"})],
            [
                _btn("🔄 Сменить линию", BLUE, {"cmd": "change_line"}),
                _btn("☕ Отлучился", WHITE, {"cmd": "driver_away"}),
            ],
            [_btn("🚪 Выйти с линии", RED, {"cmd": "leave_line"})],
        ]
    )


def pending_orders_keyboard(orders: list, page: int, total_pages: int) -> str:
    """Buttons for unclaimed requests, with a guaranteed main-menu exit."""
    rows = [
        [_btn(f"✅ Взять заявку №{order.id}", GREEN,
              {"cmd": "pending_take", "order_id": order.id})]
        for order in orders
    ]
    navigation = []
    if page > 1:
        navigation.append(_btn("⬅️ Назад", WHITE, {"cmd": "pending_orders", "page": page - 1}))
    if page < total_pages:
        navigation.append(_btn("Вперёд ➡️", WHITE, {"cmd": "pending_orders", "page": page + 1}))
    if navigation:
        rows.append(navigation)
    rows.append([_btn("⬅️ Вернуться в главное меню", WHITE, {"cmd": "start"})])
    return keyboard(rows)


def order_offer_keyboard(order_id: int) -> str:
    return keyboard(
        [
            [_btn("✅ Поехать", GREEN, {"cmd": "accept", "order_id": order_id})],
            [_btn("🚫 Отказаться", RED, {"cmd": "decline", "order_id": order_id})],
        ],
        inline=True,
    )


def decline_reasons_keyboard(order_id: int) -> str:
    reasons = [
        ("🚗 Дальние расстояния", "far"),
        ("📦 Доставка", "delivery"),
        ("📅 Бронь", "booking"),
        ("☕ Отлучился", "away"),
        ("📍 Не хватает адреса", "need_address"),
        ("🚫 Спам", "spam"),
        ("😠 Клиент мне не приятен", "dislike"),
    ]
    rows = [[_btn(label, WHITE, {"cmd": "decline_reason", "order_id": order_id, "cat": cat})]
            for label, cat in reasons]
    rows.append([_btn("⬅️ Назад", WHITE, {"cmd": "decline_back", "order_id": order_id})])
    return keyboard(rows)


def driver_ride_keyboard(
    stage: str,
    waiting: bool = False,
    eta_set: bool = False,
    has_parallel: bool = False,
    parallel_available: bool = False,
    payment_details_enabled: bool = False,
    payment_details_sent: bool = False,
    driver_gender: str | None = None,
) -> str:
    """stage: 'assigned' -> ETA/arrived; 'arrived' -> seated only;
    'in_progress' -> waiting toggle + finish. Includes the paid-waiting toggle
    (req 2) and «Отменить заказ (пассажир)» (bug #7)."""
    rows: list[list[dict]] = []
    if stage == "assigned":
        if eta_set:
            rows.append([_btn("➕ Добавить время к прибытию", WHITE, {"cmd": "eta_add_menu"})])
        else:
            rows.append([_btn("🕒 Время прибытия", WHITE, {"cmd": "set_eta"})])
        arrived_label = "🚘 Подъехала" if driver_gender == "female" else "🚘 Подъехал"
        rows.append([_btn(arrived_label, GREEN, {"cmd": "arrived"})])
    elif stage == "arrived":
        rows.append([_btn("🧍 Пассажир сел", GREEN, {"cmd": "seated"})])
    elif stage == "in_progress":
        rows.append([_btn("🏁 Завершить", GREEN, {"cmd": "finish"})])
        if payment_details_enabled and not payment_details_sent:
            rows.append([_btn("💳 Отправить реквизиты", BLUE, {"cmd": "send_payment_details"})])
        if waiting:
            rows.append([_btn("▶️ Продолжить поездку", BLUE, {"cmd": "waiting_stop"})])
        else:
            rows.append([_btn("⏳ Ожидание", WHITE, {"cmd": "waiting_start"})])
    # The final indicator is updated from actual route-compatible candidates.
    if has_parallel:
        parallel_label = "📌 Моя параллельная заявка ✅"
    else:
        parallel_label = "🔀 Параллельные заявки " + ("✅" if parallel_available else "🔴")
    rows.append([_btn(parallel_label, WHITE, {"cmd": "parallel_orders"})])
    rows.append([_btn("🏷 Прайс", WHITE, {"cmd": "price"})])
    rows.append([_btn("❌ Отменить активную заявку", RED, {"cmd":"driver_cancel_active"})])
    return keyboard(rows)


def parallel_orders_keyboard(
    orders: list[tuple[int, str]], page: int = 1, total_pages: int = 1
) -> str:
    """VK-safe paginated parallel-order buttons."""
    rows = [[_btn(f"Взять заявку #{order_id}", GREEN,
                  {"cmd": "parallel_take", "order_id": order_id})]
            for order_id, _label in orders]
    if total_pages > 1:
        navigation = []
        if page > 1:
            navigation.append(_btn("◀️ Назад", WHITE, {"cmd": "parallel_orders", "page": page - 1}))
        navigation.append(_btn(f"{page}/{total_pages}", BLUE, {"cmd": "parallel_orders", "page": page}))
        if page < total_pages:
            navigation.append(_btn("Вперёд ▶️", WHITE, {"cmd": "parallel_orders", "page": page + 1}))
        rows.append(navigation)
    rows.append([_btn("⬅️ Вернуться к активной заявке", WHITE, {"cmd": "parallel_back"})])
    return keyboard(rows)


def route_parallel_offer_keyboard(order_id: int) -> str:
    """Direct three-minute offer for a driver travelling to the pickup city."""
    return keyboard([
        [_btn("✅ Взять параллельную заявку", GREEN,
              {"cmd": "parallel_take", "order_id": order_id})],
        [_btn("❌ Отклонить", RED,
              {"cmd": "parallel_route_decline", "order_id": order_id})],
    ])


def parallel_eta_keyboard(order_id: int) -> str:
    return keyboard([
        [_btn("5 мин", BLUE, {"cmd": "parallel_eta", "order_id": order_id, "minutes": 5}),
         _btn("10 мин", BLUE, {"cmd": "parallel_eta", "order_id": order_id, "minutes": 10})],
        [_btn("15 мин", BLUE, {"cmd": "parallel_eta", "order_id": order_id, "minutes": 15}),
         _btn("20 мин", BLUE, {"cmd": "parallel_eta", "order_id": order_id, "minutes": 20})],
        [_btn("✏️ Указать своё время", WHITE,
              {"cmd": "parallel_eta_custom", "order_id": order_id})],
        [_btn("❌ Отказаться от параллельной заявки", RED,
              {"cmd": "parallel_decline", "order_id": order_id})],
        [_btn("⬅️ Вернуться к заявке", WHITE, {"cmd": "parallel_back"})],
    ])


def parallel_reserved_keyboard(order_id: int) -> str:
    return keyboard([
        [_btn("➕ Добавить время к подаче авто", BLUE,
              {"cmd": "parallel_eta_add", "order_id": order_id})],
        [_btn("❌ Отказаться от параллельной заявки", RED,
              {"cmd": "parallel_decline", "order_id": order_id})],
        [_btn("⬅️ Вернуться к активной заявке", WHITE, {"cmd": "parallel_back"})],
    ])


def fake_calls_keyboard(items: list[tuple[int, str]]) -> str:
    rows = [[_btn(f"✅ Оплачено #{fid} ({name})", GREEN, {"cmd": "fake_paid", "fc_id": fid})]
            for fid, name in items]
    rows.append([_btn("⬅️ Назад", WHITE, {"cmd": "start"})])
    return keyboard(rows)


def chat_keyboard() -> str:
    return keyboard([[_btn("🔙 Выйти из чата", WHITE, {"cmd": "exit_chat"})]])


# --------------------------------------------------------------------------- #
#  Dispatcher                                                                  #
# --------------------------------------------------------------------------- #
def dispatcher_menu(show_role_switch: bool = False) -> str:
    rows = [
        [_btn("📝 Новая заявка", GREEN, {"cmd": "disp_new_order"})],
        [_btn("📋 Мои заявки", BLUE, {"cmd": "disp_orders"})],
        [_btn("📅 Бронь", BLUE, {"cmd": "disp_booking_menu"})],
        [
            _btn("👥 Водители", WHITE, {"cmd": "drivers"}),
            _btn("🏷 Прайс", WHITE, {"cmd": "price"}),
        ],
        [_btn("💰 Мои доходы", WHITE, {"cmd": "disp_income"})],
    ]
    if show_role_switch:
        rows.append(_role_switch_row())
    return keyboard(rows)


def dispatcher_booking_menu() -> str:
    return keyboard([
        [_btn("📅 Сделать бронь", GREEN, {"cmd": "disp_booking_new"})],
        [_btn("🗓 Мои брони", BLUE, {"cmd": "disp_bookings"})],
        [_btn("⬅️ В главное меню", WHITE, {"cmd": "start"})],
    ])


def dispatcher_bookings_keyboard(
    items: list[tuple[int, str]], page: int = 1, total_pages: int = 1
) -> str:
    rows = [
        [_btn(f"❌ Отменить бронь №{booking_id} — {when}", RED,
              {"cmd": "disp_booking_cancel", "booking_id": booking_id})]
        for booking_id, when in items
    ]
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(_btn("◀️", WHITE, {"cmd": "disp_bookings", "page": page - 1}))
        nav.append(_btn(f"{page}/{total_pages}", BLUE, {"cmd": "disp_bookings", "page": page}))
        if page < total_pages:
            nav.append(_btn("▶️", WHITE, {"cmd": "disp_bookings", "page": page + 1}))
        rows.append(nav)
    rows.append([_btn("⬅️ К брони", WHITE, {"cmd": "disp_booking_menu"})])
    return keyboard(rows)


def dispatcher_orders_keyboard(
    items: list[tuple[int, str]], page: int = 1, total_pages: int = 1
) -> str:
    """VK keyboards have a row limit, so dispatcher orders are paginated."""
    rows = [
        [_btn(f"Заявка #{order_id} — {status}", WHITE,
              {"cmd": "disp_order", "order_id": order_id})]
        for order_id, status in items
    ]
    if total_pages > 1:
        navigation = []
        if page > 1:
            navigation.append(_btn("◀️ Назад", WHITE, {"cmd": "disp_orders", "page": page - 1}))
        navigation.append(_btn(f"{page}/{total_pages}", BLUE, {"cmd": "disp_orders", "page": page}))
        if page < total_pages:
            navigation.append(_btn("Вперёд ▶️", WHITE, {"cmd": "disp_orders", "page": page + 1}))
        rows.append(navigation)
    rows.append([_btn("⬅️ В главное меню", WHITE, {"cmd": "start"})])
    return keyboard(rows)


def dispatcher_order_keyboard(order_id: int) -> str:
    return keyboard([
        [_btn(f"❌ Отменить заявку #{order_id}", RED,
              {"cmd": "disp_cancel_order", "order_id": order_id})],
        [_btn("⬅️ К моим заявкам", WHITE, {"cmd": "disp_orders"})],
    ])


def order_type_keyboard_dispatcher() -> str:
    return order_type_keyboard()


# --------------------------------------------------------------------------- #
#  Admin                                                                       #
# --------------------------------------------------------------------------- #
def admin_menu(show_role_switch: bool = False) -> str:
    rows = [
        [_btn("📢 Рассылка", BLUE, {"cmd": "admin_broadcast"})],
        [
            _btn("➕ Водителя", GREEN, {"cmd": "admin_add_driver"}),
            _btn("➕ Диспетчера", GREEN, {"cmd": "admin_add_dispatcher"}),
        ],
        [
            _btn("➖ Убрать роль", RED, {"cmd": "admin_remove_role"}),
            _btn("🚫 Заблокировать", RED, {"cmd": "admin_block_user"}),
        ],
        [_btn("🕐 Суточники", BLUE, {"cmd": "admin_temp_driver"})],
    ]
    if show_role_switch:
        rows.append(_role_switch_row())
    return keyboard(rows)


def admin_remove_role_keyboard(items: list) -> str:
    """items: (vk_id, button_label, role_token). One button per role to revoke."""
    rows = [[_btn(label, RED, {"cmd": "adm_revoke", "uid": uid, "role": role})]
            for uid, label, role in items]
    rows.append([_btn("⬅️ Назад", WHITE, {"cmd": "admin_back"})])
    return keyboard(rows)


def admin_messages_keyboard(keys_titles: list[tuple[str, str]]) -> str:
    rows = [[_btn(f"✏️ {title}", WHITE, {"cmd": "edit_msg", "key": key})]
            for key, title in keys_titles]
    rows.append([_btn("⬅️ Назад", WHITE, {"cmd": "admin_back"})])
    return keyboard(rows)


def skip_photo_keyboard() -> str:
    return keyboard(
        [[_btn("🖼 Без фото / оставить", WHITE, {"cmd": "skip_photo"})]],
        one_time=True,
    )


def admin_price_keyboard(keys_titles: list[tuple[str, str]]) -> str:
    """Admin list of «Прайс» sections (root header + 3 subsections)."""
    rows = [[_btn(f"✏️ {title}", WHITE, {"cmd": "edit_price", "key": key})]
            for key, title in keys_titles]
    rows.append([_btn("⬅️ Назад", WHITE, {"cmd": "admin_back"})])
    return keyboard(rows)


def skip_price_title_keyboard() -> str:
    return keyboard(
        [[_btn("↩️ Оставить как есть", WHITE, {"cmd": "skip_price_title"})]],
        one_time=True,
    )


def skip_price_photo_keyboard() -> str:
    return keyboard(
        [[_btn("🖼 Без фото / оставить", WHITE, {"cmd": "skip_price_photo"})]],
        one_time=True,
    )


def cancel_keyboard() -> str:
    return keyboard([[_btn("❌ Отмена", RED, {"cmd": "cancel_flow"})]], one_time=True)


def driver_active_cancel_keyboard() -> str:
    return keyboard([
        [_btn("Клиент не вышел", RED, {"cmd":"driver_cancel_no_show"})],
        [_btn("Неполадка с авто", RED, {"cmd":"driver_cancel_car"})],
        [_btn("Вернуться к поездке", WHITE, {"cmd":"driver_cancel_back"})],
    ])

def chat_take_keyboard(order_id: int) -> str:
    return keyboard([
        [_btn(f"✅ Поехать по заявке №{order_id}", GREEN, {"cmd":"chat_take", "order_id":order_id})],
        [_btn("❌ Никто не согласился", RED, {"cmd":"chat_no_driver", "order_id":order_id})],
    ], inline=True)


def payment_method_keyboard() -> str:
    return keyboard([
        [_btn("По номеру телефона", BLUE, {"cmd": "payment_method", "type": "phone"})],
        [_btn("По номеру карты", WHITE, {"cmd": "payment_method", "type": "card"})],
        [_btn("❌ Отмена", RED, {"cmd": "cancel_flow"})],
    ], one_time=True)


def payment_recipient_keyboard() -> str:
    return keyboard([
        [_btn("Пропустить", WHITE, {"cmd": "payment_recipient_skip"})],
        [_btn("❌ Отмена", RED, {"cmd": "cancel_flow"})],
    ], one_time=True)


def driver_gender_keyboard() -> str:
    return keyboard([
        [_btn("👨 Мужской", BLUE, {"cmd": "set_driver_gender", "gender": "male"})],
        [_btn("👩 Женский", BLUE, {"cmd": "set_driver_gender", "gender": "female"})],
    ], one_time=True)


def driver_settings_keyboard(show_details: bool, driver_gender: str | None = None) -> str:
    toggle = "Выключить показ реквизитов" if show_details else "Включить показ реквизитов"
    gender_label = {
        "male": "👨 Пол: мужской",
        "female": "👩 Пол: женский",
    }.get(driver_gender, "👤 Указать пол")
    return keyboard([
        [_btn(gender_label, WHITE, {"cmd":"driver_gender"})],
        [_btn("🚗 Моя машина", WHITE, {"cmd":"driver_car"})],
        [_btn("💳 Реквизиты", WHITE, {"cmd":"payment_details"})],
        [_btn(toggle, BLUE, {"cmd":"payment_toggle"})],
        [_btn("⬅️ Назад", WHITE, {"cmd":"driver_settings_back"})],
    ])

def broadcast_media_keyboard() -> str:
    return keyboard([[_btn("Без медиа", WHITE, {"cmd":"broadcast_no_media"})], [_btn("Отмена", RED, {"cmd":"admin_back"})]])

def broadcast_target_keyboard() -> str:
    return keyboard([
        [_btn("Отправить всем", GREEN, {"cmd":"broadcast_send", "target":"all"})],
        [_btn("Отправить водителям", BLUE, {"cmd":"broadcast_send", "target":"driver"})],
        [_btn("Отправить пассажирам", WHITE, {"cmd":"broadcast_send", "target":"passenger"})],
        [_btn("Отмена", RED, {"cmd":"admin_back"})],
    ])
