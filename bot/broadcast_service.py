"""Asynchronous VK broadcast with batching, retries, per-user audit and report."""
from __future__ import annotations
import threading
import time
from collections import Counter
from vk_api.utils import get_random_id
from common.database import session_scope
from common.models import AdminLog, User
from common.logger import get_logger
from .vk_client import vk

log = get_logger("bot.broadcast")


def start(admin_vk_id: int, text: str, attachment: str | None, target: str = "all") -> None:
    thread = threading.Thread(target=_run, args=(admin_vk_id, text, attachment, target), daemon=True)
    thread.start()


def _reason(exc: Exception) -> str:
    raw = str(exc)
    low = raw.lower()
    if "blocked" in low or "901" in low or "can\'t send" in low:
        return "пользователь заблокировал бота"
    if "invalid" in low or "100" in low:
        return "неверный ID"
    if "flood" in low or "6" in low:
        return "лимит VK"
    return raw[:180] or exc.__class__.__name__


def _run(admin_vk_id: int, text: str, attachment: str | None, target: str) -> None:
    sent = 0
    failures: list[tuple[int, str]] = []
    try:
        with session_scope() as db:
            query = db.query(User).filter(User.vk_id.isnot(None))
            if target == "driver":
                recipients = [u.vk_id for u in query.all() if u.has_role("driver")]
            elif target == "passenger":
                recipients = [u.vk_id for u in query.all() if u.has_role("passenger")]
            else:
                recipients = [u.vk_id for u in query.all()]
        total = len(recipients)
        for offset in range(0, total, 25):
            for vk_id in recipients[offset:offset + 25]:
                error = None
                for attempt in range(3):
                    try:
                        params = {"peer_id": vk_id, "message": text[:4000], "random_id": get_random_id()}
                        if attachment:
                            params["attachment"] = attachment
                        vk.api.messages.send(**params)
                        sent += 1
                        error = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        error = _reason(exc)
                        time.sleep(0.8 * (attempt + 1))
                status = "доставлено" if error is None else f"ошибка: {error}"
                if error is not None:
                    failures.append((vk_id, error))
                with session_scope() as db:
                    db.add(AdminLog(admin_id=None, action="broadcast_delivery", details=f"admin_vk={admin_vk_id} user_vk={vk_id} status={status}"))
            time.sleep(1.0)
        counts = Counter(reason for _, reason in failures)
        reasons = "\n".join(f"• {reason}: {count}" for reason, count in counts.items()) or "• нет"
        vk.send_message(admin_vk_id, f"📊 Рассылка завершена.\nВсего пользователей: {total}\nУспешно доставлено: {sent}\nОшибок: {len(failures)}\nПричины:\n{reasons}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Broadcast failed: %s", exc)
        vk.send_message(admin_vk_id, f"Рассылка остановлена системной ошибкой: {_reason(exc)}")
