"""FSM state persistence backed by the `states` table.

State = a short string (e.g. 'passenger_from', 'driver_on_line').
Data   = arbitrary JSON payload (current draft order, etc.).
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from common.models import State


class States:
    START = "start"
    CHOOSING_ROLE = "choosing_role"
    MAIN_MENU = "main_menu"

    # passenger order creation
    P_CITY = "passenger_city"
    P_FROM = "passenger_from"
    P_TO = "passenger_to"
    # Requirement 6: both addresses entered in ONE message ("Откуда, Куда").
    P_ADDR = "passenger_addresses"
    P_PRICE = "passenger_price"
    P_COMMENT = "passenger_comment"
    P_CONFIRM = "passenger_confirm"
    P_PROMO = "passenger_promo"
    P_WAITING = "passenger_waiting"
    P_IN_RIDE = "passenger_in_ride"
    P_CHAT = "passenger_chat"
    P_RATE = "passenger_rate"
    P_TYPE = "passenger_order_type"      # choosing regular / delivery
    # Fake-call debtor lockdown: passenger can ONLY request the pay link.
    P_FAKE_CALL_LOCK = "passenger_fake_call_lock"
    P_REVIEW_TEXT = "passenger_review_text"  # optional free-text review
    P_CHAT_ORDER_CONFIRM = "passenger_chat_order_confirm"
    P_BOOKING_RULES = "passenger_booking_rules"
    P_BOOKING_TYPE = "passenger_booking_type"
    P_BOOKING_DATE = "passenger_booking_date"
    P_BOOKING_TIME = "passenger_booking_time"
    P_BOOKING_ADDRESS = "passenger_booking_address"
    P_BOOKING_EXTRAS = "passenger_booking_extras"
    P_BOOKING_COMMENT = "passenger_booking_comment"
    P_BOOKING_CONFIRM = "passenger_booking_confirm"
    P_PRICE_CALC_ROUTE = "passenger_price_calc_route"
    # delivery details FSM (requirement 6)
    P_DELIVERY_WHAT = "passenger_delivery_what"
    P_DELIVERY_WHERE = "passenger_delivery_where"
    P_DELIVERY_SUM = "passenger_delivery_sum"
    P_DELIVERY_FROM = "passenger_delivery_from"
    P_DELIVERY_TO = "passenger_delivery_to"
    P_DELIVERY_COMMENT = "passenger_delivery_comment"
    P_ARRIVED = "passenger_arrived"
    # waiting queue re-poll + address clarification (requirements 4, 5)
    P_QUEUE_CONFIRM = "passenger_queue_confirm"
    P_NEW_ADDRESS = "passenger_new_address"
    # extended features: taxi extra services + delivery price confirmation (req.1,4)
    P_EXTRAS = "passenger_extras"
    P_DELIVERY_CONFIRM = "passenger_delivery_confirm"

    # driver
    D_MENU = "driver_menu"
    D_ON_LINE = "driver_on_line"
    D_OFFER = "driver_offer"          # received an offer, deciding
    D_DECLINE_REASON = "driver_decline_reason"
    D_ETA = "driver_eta"              # entering custom arrival time (free text)
    D_ETA_ADD = "driver_eta_add"      # adding minutes after initial ETA
    # Requirement 1: preset arrival-time menu shown right after «Принять».
    D_ETA_MENU = "driver_eta_menu"
    # Requirement 5: «Моя машина» — confirm before editing existing car data.
    D_CAR_CONFIRM = "driver_car_confirm"
    D_IN_RIDE = "driver_in_ride"
    D_CHAT = "driver_chat"
    D_CHAT_ORDER_WAIT = "driver_chat_order_wait"
    D_CAR_MODEL = "driver_car_model"
    D_CAR_NUMBER = "driver_car_number"
    D_CAR_COLOR = "driver_car_color"
    D_SETTINGS = "driver_settings"
    D_GENDER = "driver_gender"
    D_PAYMENT_TYPE = "driver_payment_type"
    D_PAYMENT_PHONE = "driver_payment_phone"
    D_PAYMENT_CARD = "driver_payment_card"
    D_PAYMENT_BANK = "driver_payment_bank"
    D_PAYMENT_RECIPIENT = "driver_payment_recipient"
    D_FINISH_PRICE = "driver_finish_price"  # entering the ride price manually
    D_DELIVERY_PRICE = "driver_delivery_price"  # entering delivery price (req.4)
    D_SELECT_LINE = "driver_select_line"        # choosing a working line (req 1.1)
    D_POST_RIDE_LINE = "driver_post_ride_line"  # stay/change line after a ride
    # Requirement 3: driver rates the passenger after finishing the ride.
    D_RATE_PASSENGER = "driver_rate_passenger"
    D_RATE_PASSENGER_TEXT = "driver_rate_passenger_text"
    D_PARALLEL_ETA = "driver_parallel_eta"
    D_PARALLEL_ETA_ADD = "driver_parallel_eta_add"

    # dispatcher
    DISP_MENU = "dispatcher_menu"
    DISP_CHAT_REPLY = "dispatcher_chat_reply"

    # admin
    ADM_MENU = "admin_menu"
    ADM_ADD_DRIVER = "admin_add_driver"
    ADM_ADD_DISPATCHER = "admin_add_dispatcher"
    ADM_TEMP_DRIVER = "admin_temp_driver"
    ADM_REMOVE_ROLE = "admin_remove_role"
    ADM_BLOCK_USER = "admin_block_user"
    ADM_MSG_TEXT = "admin_msg_text"
    ADM_MSG_PHOTO = "admin_msg_photo"
    # «Прайс / Популярные направления» editing (requirement)
    ADM_PRICE_TITLE = "admin_price_title"
    ADM_PRICE_TEXT = "admin_price_text"
    ADM_PRICE_PHOTO = "admin_price_photo"
    ADM_BROADCAST_TEXT = "admin_broadcast_text"
    ADM_BROADCAST_MEDIA = "admin_broadcast_media"


def get_state(session: Session, vk_id: int) -> State:
    row = session.query(State).filter(State.vk_id == vk_id).one_or_none()
    if row is None:
        row = State(vk_id=vk_id, state=States.START, data="{}")
        session.add(row)
        session.flush()
    return row


def get_data(session: Session, vk_id: int) -> dict[str, Any]:
    row = get_state(session, vk_id)
    try:
        return json.loads(row.data) if row.data else {}
    except json.JSONDecodeError:
        return {}


def set_state(
    session: Session,
    vk_id: int,
    state: str | None = None,
    data: dict[str, Any] | None = None,
    merge: bool = True,
) -> None:
    row = get_state(session, vk_id)
    if state is not None:
        row.state = state
    if data is not None:
        if merge:
            current = {}
            try:
                current = json.loads(row.data) if row.data else {}
            except json.JSONDecodeError:
                current = {}
            current.update(data)
            row.data = json.dumps(current, ensure_ascii=False)
        else:
            row.data = json.dumps(data, ensure_ascii=False)


def reset(session: Session, vk_id: int, state: str = States.MAIN_MENU) -> None:
    row = get_state(session, vk_id)
    row.state = state
    row.data = "{}"
