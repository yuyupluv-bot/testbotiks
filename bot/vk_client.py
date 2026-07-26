"""Thin wrapper around vk_api providing a shared session, uploader and helpers.

All outgoing messages go through `send_message`, which is resilient to VK API
errors (they are logged and swallowed so a single failure never crashes the
long-poll loop).
"""
from __future__ import annotations

import datetime as dt
import contextlib
import inspect
import json
import random
import re
import threading
import time
from typing import Any

import requests
import vk_api


_PERMANENT_SEND_ERROR_CODES = {901, 911}


def _vk_error_code(exc: Exception) -> int | None:
    """Extract a VK API error code without depending on one exception class."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    match = re.search(r"\[(\d+)\]", str(exc))
    return int(match.group(1)) if match else None
from vk_api.bot_longpoll import VkBotLongPoll
from vk_api.utils import get_random_id

from common.config import config
from common.logger import get_logger

log = get_logger("bot.vk")


class VKClient:
    def __init__(self) -> None:
        self.session = vk_api.VkApi(
            token=config.VK_TOKEN, api_version=config.VK_API_VERSION
        )
        self.api = self.session.get_api()
        self.uploader = vk_api.VkUpload(self.session)
        # Long Poll uses a separate HTTP session so worker API calls never race
        # with the listener's blocking request.
        self.longpoll_session = vk_api.VkApi(
            token=config.VK_TOKEN, api_version=config.VK_API_VERSION
        )
        self.longpoll = VkBotLongPoll(self.longpoll_session, group_id=config.VK_GROUP_ID)
        # vk_api uses one requests session. Serialize access to that session;
        # event handling itself remains concurrent in bot.main.
        self._api_lock = threading.RLock()
        # Each outbox thread gets an independent VK HTTP session. This removes
        # the global outgoing-message bottleneck without sharing sessions.
        self._message_local = threading.local()

    def validate_startup(self) -> dict[str, Any]:
        """Fail loudly when the group/token/Long Poll pair is unusable."""
        if not config.VK_TOKEN:
            raise RuntimeError("VK_TOKEN is empty")
        if config.VK_GROUP_ID <= 0:
            raise RuntimeError("VK_GROUP_ID must be a positive numeric community id")
        with self._api_lock:
            settings = self.api.groups.getLongPollSettings(group_id=config.VK_GROUP_ID)
            # This method requires community message permission and catches a
            # valid token that belongs to the wrong group or lacks messages.
            self.api.messages.getConversations(count=1)
        events = settings.get("events", {}) if isinstance(settings, dict) else {}
        if not settings or not settings.get("is_enabled"):
            raise RuntimeError("VK community Long Poll API is disabled")
        if not events.get("message_new"):
            raise RuntimeError("VK Long Poll event message_new is disabled")
        if not events.get("message_reply"):
            log.error(
                "VK Long Poll event message_reply is disabled; enable "
                "Outgoing messages in community Long Poll event types"
            )
        if config.VK_CHAT_USER_TOKEN:
            try:
                self._message_api_for_peer(2_000_000_001).messages.getConversations(count=1)
                log.info("VK chat user token validated for chat message deletion")
            except Exception as exc:  # noqa: BLE001
                log.warning("VK_CHAT_USER_TOKEN cannot access messages: %s", exc)
        return settings

    def _message_api(self):
        api = getattr(self._message_local, "api", None)
        if api is None:
            session = vk_api.VkApi(
                token=config.VK_TOKEN,
                api_version=config.VK_API_VERSION,
            )
            self._message_local.session = session
            api = session.get_api()
            self._message_local.api = api
        return api

    def _message_api_for_peer(self, peer_id: int):
        """Use a user token for chats so VK returns a deletable message id."""
        if peer_id >= 2_000_000_000 and config.VK_CHAT_USER_TOKEN:
            api = getattr(self._message_local, "chat_user_api", None)
            if api is None:
                session = vk_api.VkApi(
                    token=config.VK_CHAT_USER_TOKEN,
                    api_version=config.VK_API_VERSION,
                )
                self._message_local.chat_user_session = session
                api = session.get_api()
                self._message_local.chat_user_api = api
            return api
        return self._message_api()

    @staticmethod
    def _uses_chat_user_token(peer_id: int) -> bool:
        return bool(peer_id >= 2_000_000_000 and config.VK_CHAT_USER_TOKEN)

    # -- Messaging ----------------------------------------------------------
    @staticmethod
    def _message_priority(text: str) -> int:
        value = (text or "").lstrip()
        if value.startswith("🔔 Новая заявка #"):
            return 100
        if value.startswith("Есть заявка в боте") or re.match(r"^Есть \d+ заяв", value):
            return 80
        if "Нашёлся свободный водитель" in value or "Водитель прибудет" in value:
            return 50
        # Payment details must never sit behind menus/status updates or be
        # cancelled by the per-peer low-priority backlog guard.
        if "Реквизиты для оплаты" in value:
            return 5_000
        # Completion and parallel transition are emitted in one transaction.
        # Give completion an absolute outbox priority so it is delivered first,
        # even when a worker claims the batch immediately after commit.
        if value.startswith("✅ Поездка #"):
            return 10_000
        if value.startswith("Переходим к заявке #"):
            return -100
        # The driver must see the confirmation/queue-position message before
        # an immediately available offer queued in the same transaction.
        if "Вы остались на линии «" in value:
            return 1_000
        if "Вы первый в очереди!" in value:
            return 900
        return 0

    def send_message(
        self,
        peer_id: int,
        text: str = "",
        keyboard: str | None = None,
        attachment: str | None = None,
    ) -> bool:
        if peer_id < 0:
            # Telegram passengers keep a synthetic negative vk_id so the same
            # passenger FSM can be reused while drivers remain in VK.
            from bot import telegram_messaging
            return telegram_messaging.enqueue_for_synthetic_vk_id(
                peer_id, text=text, keyboard=keyboard, attachment=attachment
            )
        if peer_id == 0:
            log.error("Refusing to send VK message to invalid peer_id=%s", peer_id)
            return False
        # Apply the admin-edited template for this exact active callsite. One
        # cached BotMessage query serves all sends for 30 seconds.
        try:
            caller = inspect.currentframe().f_back
            if caller is not None:
                from common import bot_messages_service as bm
                text = bm.render_outgoing(
                    text or "",
                    caller.f_code.co_filename,
                    caller.f_code.co_name,
                    caller.f_lineno,
                    caller.f_locals,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Bot text override was skipped: %s", exc)
        # Inside a bot DB transaction, persist the message atomically with the
        # state change. The outbox worker sends it only after commit.
        try:
            from common.database import current_session
            from common.models import OutboxMessage
            db = current_session()
            if db is not None:
                clean_text = (text or "")[:4000]
                priority = self._message_priority(clean_text)
                now = dt.datetime.now(dt.timezone.utc)
                # Repeated callbacks must not build an identical unsent queue.
                duplicate = (
                    db.query(OutboxMessage.id)
                    .filter(
                        OutboxMessage.peer_id == peer_id,
                        OutboxMessage.status.in_(("pending", "failed", "sending")),
                        OutboxMessage.text == clean_text,
                        OutboxMessage.keyboard == keyboard,
                        OutboxMessage.attachment == attachment,
                        OutboxMessage.created_at >= now - dt.timedelta(seconds=5),
                    )
                    .first()
                )
                if duplicate:
                    log.info("Suppressed duplicate queued VK message for peer_id=%s", peer_id)
                    return True

                pending = int(
                    db.query(OutboxMessage.id)
                    .filter(
                        OutboxMessage.peer_id == peer_id,
                        OutboxMessage.status.in_(("pending", "failed", "sending")),
                    )
                    .count()
                )
                # Keep the newest menu/status response when one peer has an
                # abnormal backlog. Critical order messages (priority > 0)
                # are never removed by this guard.
                if pending >= 20 and priority <= 0:
                    excess = pending - 19
                    stale_rows = (
                        db.query(OutboxMessage)
                        .filter(
                            OutboxMessage.peer_id == peer_id,
                            OutboxMessage.status.in_(("pending", "failed")),
                            OutboxMessage.priority <= 0,
                        )
                        .order_by(OutboxMessage.id.asc())
                        .limit(excess)
                        .all()
                    )
                    for stale in stale_rows:
                        stale.status = "cancelled"
                        stale.claimed_at = None
                        stale.last_error = "cancelled by per-peer outbox cap"
                    if stale_rows:
                        log.warning(
                            "Outbox cap for peer_id=%s: cancelled %s stale low-priority messages",
                            peer_id,
                            len(stale_rows),
                        )

                db.add(OutboxMessage(
                    peer_id=peer_id,
                    text=clean_text,
                    keyboard=keyboard,
                    attachment=attachment,
                    random_id=get_random_id(),
                    status="pending",
                    priority=priority,
                    next_attempt_at=now,
                ))
                return True
        except Exception as exc:  # noqa: BLE001
            log.error("Could not enqueue VK message for %s: %s", peer_id, exc)
            return False
        return self._send_now(peer_id, text, keyboard, attachment, get_random_id())

    def send_tracked_message(
        self,
        peer_id: int,
        text: str = "",
        keyboard: str | None = None,
        attachment: str | None = None,
    ) -> int | None:
        """Queue a message and return its outbox id for later deletion."""
        try:
            from common.database import current_session
            from common.models import OutboxMessage
            db = current_session()
            if db is None or peer_id <= 0:
                return None
            row = OutboxMessage(
                peer_id=peer_id,
                text=(text or "")[:4000],
                keyboard=keyboard,
                attachment=attachment,
                random_id=get_random_id(),
                status="pending",
                priority=self._message_priority(text),
                next_attempt_at=dt.datetime.now(dt.timezone.utc),
            )
            db.add(row)
            db.flush()
            return int(row.id)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not enqueue tracked VK message for %s: %s", peer_id, exc)
            return None

    def _send_now(
        self,
        peer_id: int,
        text: str = "",
        keyboard: str | None = None,
        attachment: str | None = None,
        random_id: int | None = None,
    ) -> bool:
        return self._send_now_result(peer_id, text, keyboard, attachment, random_id) is not None

    def _resolve_sent_chat_cmid(
        self,
        peer_id: int,
        random_id: int | None,
        text: str,
        keyboard: str | None = None,
        attachment: str | None = None,
        previous_cmid: int | None = None,
    ) -> int | None:
        """Recover a chat cmid when messages.send returns message_id=0 only."""
        def _matches(message) -> bool:
            if not isinstance(message, dict):
                return False
            value = str(message.get("text") or "")
            from_id = int(message.get("from_id") or 0)
            group_id = abs(int(config.VK_GROUP_ID or 0))
            ours = bool(message.get("out")) or abs(from_id) == group_id
            if not ours or " ".join(value.split()) != " ".join((text or "")[:4000].split()):
                return False
            returned_random = message.get("random_id")
            return not returned_random or random_id is None or int(returned_random) == int(random_id)

        try:
            result = self._message_api_for_peer(peer_id).messages.getConversationsById(
                peer_ids=str(peer_id),
            )
            items = result.get("items", []) if isinstance(result, dict) else []
            for item in items:
                last = item.get("last_message") if isinstance(item, dict) else None
                if _matches(last):
                    cmid = last.get("conversation_message_id")
                    if str(cmid or "").isdigit():
                        return int(cmid)
                # The conversation call is available with the community token
                # even when getHistory returns VK error 15. Immediately after
                # our serialized send, a newer community-owned last message is
                # authoritative even if VK normalized its displayed text.
                if isinstance(last, dict):
                    cmid = last.get("conversation_message_id")
                    from_id = int(last.get("from_id") or 0)
                    ours = bool(last.get("out")) or abs(from_id) == abs(int(config.VK_GROUP_ID or 0))
                    if ours and str(cmid or "").isdigit() and int(cmid) > int(previous_cmid or 0):
                        return int(cmid)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not resolve VK cmid from conversation peer=%s: %s", peer_id, exc)

        # Strong fallback: probe the small cmid range created since the
        # pre-send snapshot. VK only lets the community edit its own message,
        # so a successful no-op edit proves the exact cmid without history.
        if previous_cmid and text:
            for candidate in range(int(previous_cmid) + 1, int(previous_cmid) + 13):
                try:
                    params: dict[str, Any] = {
                        "peer_id": peer_id,
                        "group_id": config.VK_GROUP_ID,
                        "conversation_message_id": candidate,
                        "message": (text or "")[:4000],
                    }
                    if keyboard is not None:
                        params["keyboard"] = keyboard
                    if attachment:
                        params["attachment"] = attachment
                    result = self._message_api_for_peer(peer_id).messages.edit(**params)
                    if result is not False:
                        log.info("Verified VK chat cmid=%s peer=%s by no-op edit", candidate, peer_id)
                        return candidate
                except Exception:  # wrong/non-owned candidate is expected
                    continue

        # Final fallback for installations where history is permitted.
        # after the bot. Search only a short recent window for the exact text.
        try:
            result = self._message_api_for_peer(peer_id).messages.getHistory(peer_id=peer_id, count=50)
            items = result.get("items", []) if isinstance(result, dict) else []
            for item in items:
                if _matches(item):
                    cmid = item.get("conversation_message_id")
                    if str(cmid or "").isdigit():
                        return int(cmid)
        except Exception as exc:  # noqa: BLE001
            if _vk_error_code(exc) != 15:
                log.warning("Could not resolve VK cmid from history peer=%s: %s", peer_id, exc)
        return None

    def _last_chat_cmid(self, peer_id: int) -> int | None:
        """Snapshot the conversation sequence before sending."""
        try:
            result = self._message_api_for_peer(peer_id).messages.getConversationsById(peer_ids=str(peer_id))
            items = result.get("items", []) if isinstance(result, dict) else []
            for item in items:
                last = item.get("last_message") if isinstance(item, dict) else None
                cmid = last.get("conversation_message_id") if isinstance(last, dict) else None
                if str(cmid or "").isdigit():
                    return int(cmid)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not snapshot VK chat cmid peer=%s: %s", peer_id, exc)
        return None

    def _send_now_result(
        self,
        peer_id: int,
        text: str = "",
        keyboard: str | None = None,
        attachment: str | None = None,
        random_id: int | None = None,
    ) -> int | None:
        self._message_local.last_send_error = None
        self._message_local.last_conversation_message_id = None
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "random_id": random_id if random_id is not None else get_random_id(),
            # VK then returns both the global message id and the chat cmid.
            "return_response": 1,
        }
        if text:
            params["message"] = text[:4000]
        if keyboard is not None:
            params["keyboard"] = keyboard
        if attachment:
            params["attachment"] = attachment
        for attempt in range(3):
            try:
                lock = self._api_lock if peer_id >= 2_000_000_000 else contextlib.nullcontext()
                with lock:
                    previous_cmid = self._last_chat_cmid(peer_id) if peer_id >= 2_000_000_000 else None
                    result = self._message_api_for_peer(peer_id).messages.send(**params)
                    # vk_api normally unwraps `response`, but preserve support
                    # for proxies/API versions that return one extra envelope.
                    while isinstance(result, dict) and "response" in result:
                        result = result.get("response")
                    if isinstance(result, (list, tuple)):
                        result = result[0] if result else None
                    if isinstance(result, dict):
                        cmid = result.get("conversation_message_id")
                        if str(cmid or "").isdigit():
                            self._message_local.last_conversation_message_id = int(cmid)
                        # Preserve message_id=0. In community chats VK commonly
                        # returns only a usable conversation_message_id; treating
                        # zero as falsy used to mislabel the cmid as a global id.
                        message_id = result.get("message_id")
                        if message_id is None:
                            message_id = result.get("id")
                        result = message_id if message_id is not None else cmid
                    if (
                        peer_id >= 2_000_000_000
                        and not self.last_conversation_message_id()
                    ):
                        for resolve_attempt in range(3):
                            recovered = self._resolve_sent_chat_cmid(
                                peer_id,
                                params.get("random_id"),
                                text,
                                keyboard,
                                attachment,
                                previous_cmid,
                            )
                            if recovered:
                                self._message_local.last_conversation_message_id = recovered
                                log.info("Resolved VK chat cmid=%s peer=%s", recovered, peer_id)
                                break
                            if resolve_attempt < 2:
                                time.sleep(0.1 * (resolve_attempt + 1))
                self._message_local.last_send_error = None
                return int(result)
            except Exception as exc:  # noqa: BLE001 - never crash the loop
                error_code = _vk_error_code(exc)
                if error_code in _PERMANENT_SEND_ERROR_CODES:
                    self._message_local.last_send_error = str(exc)
                    log.error(
                        "Permanent VK send failure for %s, no retry: %s",
                        peer_id,
                        exc,
                    )
                    return None
                if attempt == 2:
                    self._message_local.last_send_error = str(exc)
                    log.error("Failed to send message to %s after retries: %s", peer_id, exc)
                    return None
                time.sleep(0.15 * (attempt + 1))
        return None

    def last_send_error(self) -> str | None:
        return getattr(self._message_local, "last_send_error", None)

    def last_conversation_message_id(self) -> int | None:
        return getattr(self._message_local, "last_conversation_message_id", None)

    def edit_message(
        self,
        peer_id: int,
        message_id: int | None,
        text: str,
        keyboard: str | None = None,
        conversation_message_id: int | None = None,
    ) -> bool:
        """Edit a sent message using its exact VK id namespace."""
        if peer_id <= 0 or (not conversation_message_id and not (message_id and message_id > 0)):
            return False
        attempts: list[tuple[str, int]] = []
        if conversation_message_id:
            attempts.append(("conversation_message_id", conversation_message_id))
        if message_id and message_id > 0:
            attempts.append(("message_id", message_id))
        if not conversation_message_id and message_id and message_id > 0:
            attempts.append(("conversation_message_id", message_id))
        errors: list[str] = []
        for id_field, value in attempts:
            group_modes = (False, True) if self._uses_chat_user_token(peer_id) else (True, False)
            for include_group_id in group_modes:
                try:
                    params = {"peer_id": peer_id, id_field: value, "message": text}
                    if include_group_id:
                        params["group_id"] = config.VK_GROUP_ID
                    if keyboard is not None:
                        params["keyboard"] = keyboard
                    result = self._message_api_for_peer(peer_id).messages.edit(**params)
                    if result is not False:
                        return True
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{id_field}/group={include_group_id}: {exc}")
        log.error("Failed to edit VK message peer=%s message=%s: %s", peer_id, message_id, "; ".join(errors))
        return False

    def edit_message_keyboard(self, peer_id: int, message_id: int, keyboard: str) -> bool:
        return self.edit_message(peer_id, message_id, "", keyboard)

    def delete_message(
        self,
        peer_id: int,
        message_id: int | None,
        conversation_message_id: int | None = None,
    ) -> bool:
        """Delete the community's own message for everyone, with retries."""
        def _deleted(result) -> bool:
            if isinstance(result, dict):
                return any(bool(value) for value in result.values())
            return bool(result)

        for attempt in range(3):
            if conversation_message_id:
                errors: list[str] = []
                for include_group_id in (True, False):
                    try:
                        params = {
                            "peer_id": peer_id,
                            "cmids": str(conversation_message_id),
                            "delete_for_all": 1,
                        }
                        if include_group_id:
                            params["group_id"] = config.VK_GROUP_ID
                        result = self._message_api_for_peer(peer_id).messages.delete(**params)
                        if _deleted(result):
                            log.info(
                                "Deleted VK cmid=%s peer=%s for all",
                                conversation_message_id,
                                peer_id,
                            )
                            return True
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc))
                if attempt == 2:
                    log.warning(
                        "VK cmid delete failed peer=%s cmid=%s: %s",
                        peer_id,
                        conversation_message_id,
                        "; ".join(errors),
                    )
            if message_id and message_id > 0:
                global_errors: list[str] = []
                group_modes = (False, True) if self._uses_chat_user_token(peer_id) else (True, False)
                for include_group_id in group_modes:
                    try:
                        params = {"message_ids": str(message_id), "delete_for_all": 1}
                        if include_group_id:
                            params["group_id"] = config.VK_GROUP_ID
                        result = self._message_api_for_peer(peer_id).messages.delete(**params)
                        if _deleted(result):
                            log.info("Deleted VK message_id=%s peer=%s for all", message_id, peer_id)
                            return True
                    except Exception as exc:  # noqa: BLE001
                        global_errors.append(str(exc))
                if attempt == 2:
                    log.warning(
                        "Global VK delete failed peer=%s message=%s: %s",
                        peer_id, message_id, "; ".join(global_errors),
                    )
            if not conversation_message_id and message_id and message_id > 0:
                try:
                    result = self._message_api_for_peer(peer_id).messages.delete(
                        peer_id=peer_id,
                        group_id=config.VK_GROUP_ID,
                        cmids=str(message_id),
                        delete_for_all=1,
                    )
                    if _deleted(result):
                        log.info("Deleted fallback VK cmid=%s peer=%s for all", message_id, peer_id)
                        return True
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        log.error("VK fallback delete failed peer=%s message=%s: %s", peer_id, message_id, exc)
            time.sleep(0.25 * (attempt + 1))
        return False

    def delete_messages_by_prefix(self, peer_id: int, prefix: str) -> bool:
        """Delete this community's matching messages directly from chat history.

        This is the recovery path when an older release lost the outbox/VK id
        mapping. Conversation message ids from getHistory are authoritative for
        chat deletion, unlike the ambiguous scalar returned by messages.send.
        """
        if peer_id <= 0 or not prefix:
            return False
        try:
            cmids: list[int] = []
            for offset in range(0, 1000, 200):
                history = self._message_api_for_peer(peer_id).messages.getHistory(
                    peer_id=peer_id,
                    group_id=config.VK_GROUP_ID,
                    count=200,
                    offset=offset,
                )
                items = history.get("items", []) if isinstance(history, dict) else []
                if not items:
                    break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    value = str(item.get("text") or "")
                    from_id = int(item.get("from_id") or 0)
                    is_ours = bool(item.get("out")) or (
                        config.VK_GROUP_ID and from_id == -int(config.VK_GROUP_ID)
                    )
                    cmid = item.get("conversation_message_id")
                    if is_ours and value.startswith(prefix) and str(cmid or "").isdigit():
                        cmids.append(int(cmid))
                if len(items) < 200:
                    break
            unique = sorted(set(cmids))
            for index in range(0, len(unique), 100):
                batch = unique[index:index + 100]
                result = self._message_api_for_peer(peer_id).messages.delete(
                    peer_id=peer_id,
                    group_id=config.VK_GROUP_ID,
                    cmids=",".join(str(value) for value in batch),
                    delete_for_all=1,
                )
                if isinstance(result, dict) and not all(bool(value) for value in result.values()):
                    raise RuntimeError(f"VK did not delete every cmid: {result}")
            if unique:
                log.info("Deleted %s old aggregate notices from peer=%s", len(unique), peer_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to clean aggregate notices peer=%s: %s", peer_id, exc)
            return False

    def get_user_info(self, vk_id: int) -> dict[str, Any]:
        try:
            with self._api_lock:
                res = self.api.users.get(user_ids=vk_id, fields="first_name,last_name,sex")
            if res:
                return res[0]
        except Exception as exc:  # noqa: BLE001
            log.error("users.get failed for %s: %s", vk_id, exc)
        return {}


    def is_group_member(self, vk_id: int) -> bool | None:
        if not config.VK_GROUP_ID:
            return None
        try:
            with self._api_lock:
                value = self.api.groups.isMember(group_id=config.VK_GROUP_ID, user_id=vk_id)
            if isinstance(value, dict):
                value = value.get("member")
            return bool(value)
        except Exception as exc:  # noqa: BLE001
            log.error("groups.isMember failed for %s: %s", vk_id, exc)
            return None

    def full_name(self, vk_id: int) -> str:
        info = self.get_user_info(vk_id)
        name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
        return name or f"id{vk_id}"

    def resolve_user_id(self, ref):
        """Resolve a VK user id from a raw input: a profile link
        (https://vk.com/brodiaga59 or https://vk.ru/brodiaga59), a short name,
        an @mention, «id123» or a plain numeric id. Screen names are resolved
        via the VK API."""
        token = (ref or "").strip()
        if not token:
            return None
        m = re.search(
            r"(?:https?://)?(?:www\.|m\.)?vk\.(?:com|ru)/([^/?#\s\]]+)",
            token,
            flags=re.IGNORECASE,
        )
        if m:
            token = m.group(1)
        token = token.lstrip("@").strip()
        if token.lower().startswith("id") and token[2:].isdigit():
            return int(token[2:])
        if token.isdigit():
            return int(token)
        try:
            with self._api_lock:
                res = self.api.users.get(user_ids=token, fields="first_name,last_name")
            if res:
                return int(res[0]["id"])
        except Exception as exc:  # noqa: BLE001
            log.error("resolve_user_id failed for %s: %s", token, exc)
        return None

    # -- Attachments --------------------------------------------------------
    def message_attachments(self, message: dict) -> list[dict]:
        """Return complete attachments, hydrating sparse Long Poll events.

        VK can deliver a voice event with an empty/short attachment list. For
        empty-text media messages we fetch the full message by conversation id
        before routing it through the passenger/driver FSM.
        """
        raw = message.get("attachments") or []
        if isinstance(raw, (list, tuple)) and raw:
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict) and raw:
            values = [item for item in raw.values() if isinstance(item, dict)]
            if values:
                return values
        peer_id = int(message.get("peer_id") or message.get("from_id") or 0)
        conversation_id = int(message.get("conversation_message_id") or 0)
        message_id = int(message.get("id") or 0)
        if peer_id <= 0 or (conversation_id <= 0 and message_id <= 0):
            return []
        for attempt in range(3):
            try:
                with self._api_lock:
                    if conversation_id:
                        response = self.api.messages.getByConversationMessageId(
                            peer_id=peer_id,
                            conversation_message_ids=conversation_id,
                        )
                    else:
                        response = self.api.messages.getById(message_ids=message_id)
                items = response.get("items", []) if isinstance(response, dict) else []
                if items:
                    hydrated = items[0].get("attachments") or []
                    if isinstance(hydrated, list):
                        return [item for item in hydrated if isinstance(item, dict)]
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log.error("Could not hydrate VK attachments: %s", exc)
                else:
                    time.sleep(0.15 * (attempt + 1))
        return []

    @staticmethod
    def _voice_url(voice: dict) -> str | None:
        preview = (voice.get("preview") or {}).get("audio_msg") or {}
        return (
            voice.get("link_ogg")
            or voice.get("link_mp3")
            or voice.get("url")
            or preview.get("link_ogg")
            or preview.get("link_mp3")
        )

    def reupload_attachments(self, peer_id: int, attachments: list[dict]) -> list[str]:
        """Download incoming attachments and re-upload them so they can be
        forwarded to another user. Returns a list of attachment strings
        (e.g. ``photo123_456``).
        """
        result: list[str] = []
        for att in attachments:
            try:
                att_type = att.get("type")
                if att_type == "photo":
                    result.append(self._reupload_photo(peer_id, att["photo"]))
                elif att_type == "audio_message":
                    # Foreign voice ids are silently omitted by VK for another
                    # peer, so upload a fresh community-owned copy.
                    result.append(self._reupload_voice(peer_id, att["audio_message"]))
                elif att_type == "doc":
                    doc = att["doc"]
                    if int(doc.get("type") or 0) == 5 or (doc.get("preview") or {}).get("audio_msg"):
                        result.append(self._reupload_voice(peer_id, att["doc"]))
                    else:
                        result.append(self._reupload_doc(peer_id, att["doc"]))
                else:
                    # sticker, audio_message, wall etc. can often be forwarded
                    # by their owner_id_id reference directly.
                    owner = att[att_type].get("owner_id")
                    obj_id = att[att_type].get("id")
                    access = att[att_type].get("access_key")
                    ref = f"{att_type}{owner}_{obj_id}"
                    if access:
                        ref += f"_{access}"
                    result.append(ref)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not re-upload attachment: %s", exc)
        return result

    @staticmethod
    def voice_attachment_reference(attachments: list[dict] | None) -> str | None:
        """Preserve voice source data until the receiving peer is known."""
        for att in attachments or []:
            att_type = att.get("type")
            if att_type == "audio_message":
                value = att.get("audio_message") or {}
            elif att_type == "doc":
                doc = att.get("doc") or {}
                if int(doc.get("type") or 0) != 5 and not (doc.get("preview") or {}).get("audio_msg"):
                    continue
                value = doc
            else:
                continue
            source_url = VKClient._voice_url(value)
            if source_url:
                return json.dumps({"vk_voice": value}, ensure_ascii=False)
            owner = value.get("owner_id")
            obj_id = value.get("id")
            if owner is None or obj_id is None:
                continue
            ref = f"doc{owner}_{obj_id}"
            access = value.get("access_key")
            if access:
                ref += f"_{access}"
            return ref
        return None

    def prepare_voice_attachment(self, peer_id: int, stored: str | None) -> str | None:
        """Create a real attachment owned by the community for this peer."""
        if not stored:
            return None
        try:
            payload = json.loads(stored)
        except (TypeError, ValueError, json.JSONDecodeError):
            return stored
        voice = payload.get("vk_voice") if isinstance(payload, dict) else None
        if not isinstance(voice, dict):
            return stored
        try:
            return self._reupload_voice(peer_id, voice)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not prepare voice attachment for peer=%s: %s", peer_id, exc)
            return None

    @staticmethod
    def _saved_doc_reference(saved: object) -> str:
        value = saved
        if isinstance(value, list):
            value = value[0] if value else {}
        if isinstance(value, dict):
            value = value.get("audio_message") or value.get("doc") or value
        if not isinstance(value, dict) or value.get("owner_id") is None or value.get("id") is None:
            raise ValueError("VK did not return the saved voice document")
        ref = f"doc{value['owner_id']}_{value['id']}"
        if value.get("access_key"):
            ref += f"_{value['access_key']}"
        return ref

    def _reupload_voice(self, peer_id: int, voice: dict) -> str:
        """Upload an incoming voice as a playable VK voice attachment."""
        url = self._voice_url(voice)
        if not url:
            raise ValueError("voice source URL is missing")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content = response.content

        try:
            with self._api_lock:
                server = self.api.docs.getMessagesUploadServer(
                    type="audio_message", peer_id=peer_id
                )
            uploaded = requests.post(
                server["upload_url"],
                files={"file": ("voice_message.ogg", content, "audio/ogg")},
                timeout=30,
            )
            uploaded.raise_for_status()
            with self._api_lock:
                saved = self.api.docs.save(file=uploaded.json()["file"])
            return self._saved_doc_reference(saved)
        except Exception as exc:  # noqa: BLE001
            log.warning("audio_message upload failed, using document fallback: %s", exc)

        tmp = f"/tmp/vk_{get_random_id()}_voice_message.ogg"
        with open(tmp, "wb") as fh:
            fh.write(content)
        saved = self.uploader.document_message(
            tmp, peer_id=peer_id, title="Голосовое сообщение.ogg"
        )
        return self._saved_doc_reference(saved)

    def _reupload_photo(self, peer_id: int, photo: dict) -> str:
        # pick the largest available size
        sizes = sorted(photo["sizes"], key=lambda s: s.get("width", 0))
        url = sizes[-1]["url"]
        content = requests.get(url, timeout=20).content
        tmp = f"/tmp/vk_{get_random_id()}.jpg"
        with open(tmp, "wb") as fh:
            fh.write(content)
        saved = self.uploader.photo_messages(photos=tmp, peer_id=peer_id)[0]
        return f"photo{saved['owner_id']}_{saved['id']}_{saved['access_key']}"

    def _reupload_doc(self, peer_id: int, doc: dict) -> str:
        url = doc["url"]
        content = requests.get(url, timeout=30).content
        title = doc.get("title", "file")
        tmp = f"/tmp/vk_{get_random_id()}_{title}"
        with open(tmp, "wb") as fh:
            fh.write(content)
        saved = self.uploader.document_message(tmp, peer_id=peer_id, title=title)
        d = saved["doc"]
        return f"doc{d['owner_id']}_{d['id']}"

    # -- Anti-fraud stats (requirement 2) -----------------------------------
    def get_account_stats(self, vk_id: int) -> dict[str, Any]:
        """Return the best-effort account age used for verification.

        Missing values come back as None so the caller can skip that gate
        instead of blocking a genuine user on an API error.
        """
        return {
            "account_age_days": self._registration_age_days(vk_id),
        }

    def _registration_age_days(self, vk_id: int) -> int | None:
        """Best-effort account age via the public foaf.php endpoint.

        VK's users.get does not expose the registration date, so we parse the
        ``ya:created dc:date`` field from foaf.php. Any failure returns None.
        """
        try:
            url = "https://vk.com/foaf.php?id=" + str(vk_id)
            resp = requests.get(url, timeout=3)
            match = re.search(r'created dc:date="([0-9T:+\-]+)"', resp.text)
            if not match:
                return None
            created = dt.datetime.fromisoformat(match.group(1))
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt.timezone.utc)
            return (dt.datetime.now(dt.timezone.utc) - created).days
        except Exception as exc:  # noqa: BLE001
            log.warning("registration-date lookup failed for %s: %s", vk_id, exc)
            return None


# Single shared instance
vk = VKClient()
