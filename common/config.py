"""Central configuration loaded from environment variables (.env).

Both the bot and the web admin import from here so there is a single source
of truth for credentials and tunables.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    # python-dotenv is optional in production (Vercel/systemd inject env vars),
    # but very convenient locally.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a soft dependency
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _get(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"See .env.example for the full list."
        )
    return value


def _parse_ids(raw: str | None) -> list[int]:
    """Parse integer IDs from a comma/space/semicolon-separated string."""
    if not raw:
        return []
    ids: list[int] = []
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            ids.append(int(chunk))
    return ids


class Config:
    # --- Database -----------------------------------------------------------
    # NOTE: production MUST be PostgreSQL. Example:
    #   postgresql+psycopg2://user:password@host:5432/dbname
    DATABASE_URL: str = _get(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:postgres@localhost:5432/taxi",
    )  # type: ignore[assignment]

    # SQLAlchemy pool settings tuned for serverless (Vercel) + long-poll bot.
    DB_POOL_SIZE: int = int(_get("DB_POOL_SIZE", "10"))
    DB_MAX_OVERFLOW: int = int(_get("DB_MAX_OVERFLOW", "10"))
    DB_POOL_RECYCLE: int = int(_get("DB_POOL_RECYCLE", "280"))
    DB_POOL_TIMEOUT: int = int(_get("DB_POOL_TIMEOUT", "10"))

    # Parallel event processing for 100+ users per minute.
    BOT_WORKERS: int = max(4, int(_get("BOT_WORKERS", "16")))
    BOT_MAX_PENDING_EVENTS: int = max(100, int(_get("BOT_MAX_PENDING_EVENTS", "1000")))
    OUTBOX_WORKERS: int = max(4, int(_get("OUTBOX_WORKERS", "8")))
    TIMER_WORKERS: int = max(2, int(_get("TIMER_WORKERS", "8")))
    SETTINGS_CACHE_TTL: int = max(5, int(_get("SETTINGS_CACHE_TTL", "30")))
    MAINTENANCE_INTERVAL: int = max(30, int(_get("MAINTENANCE_INTERVAL", "60")))
    PROCESSED_EVENTS_RETENTION_HOURS: int = max(1, int(_get("PROCESSED_EVENTS_RETENTION_HOURS", "12")))
    OUTBOX_RETENTION_HOURS: int = max(1, int(_get("OUTBOX_RETENTION_HOURS", "24")))

    # --- VK -----------------------------------------------------------------
    VK_TOKEN: str = _get("VK_TOKEN", "")  # type: ignore[assignment]
    # User token used only for the unified requests chat. Community tokens can
    # return message_id=0 and no cmid there, making deletion impossible.
    VK_CHAT_USER_TOKEN: str = _get("VK_CHAT_USER_TOKEN", "")
    VK_GROUP_ID: int = int(_get("VK_GROUP_ID", "0"))
    VK_API_VERSION: str = _get("VK_API_VERSION", "5.199")

    # --- Web admin ----------------------------------------------------------
    SECRET_KEY: str = _get("SECRET_KEY", "")
    ADMIN_LOGIN: str = _get("ADMIN_LOGIN", "admin")
    # bcrypt/werkzeug hash of the initial admin password (used by seed script).
    ADMIN_PASSWORD_HASH: str = _get("ADMIN_PASSWORD_HASH", "")
    # Plaintext admin password. Convenient on Vercel: set ADMIN_PASSWORD
    # instead of pre-generating a hash. Used as a login fallback.
    ADMIN_PASSWORD: str = _get("ADMIN_PASSWORD", "")
    # VK IDs bootstrapped as administrators on first contact (requirement 11).
    ADMIN_VK_IDS: list[int] = _parse_ids(_get("ADMIN_VK_IDS", ""))

    # --- Dispatcher ---------------------------------------------------------
    # Commission share a driver owes the dispatcher (fraction of price). 10%.
    DISPATCHER_COMMISSION: float = float(_get("DISPATCHER_COMMISSION", "0.10"))

    # --- Anti-fraud verification (requirement 2) ----------------------------
    VERIFY_ENABLED: bool = _get("VERIFY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    VERIFY_MIN_FRIENDS: int = int(_get("VERIFY_MIN_FRIENDS", "10"))
    VERIFY_MIN_ACCOUNT_MONTHS: int = int(_get("VERIFY_MIN_ACCOUNT_MONTHS", "6"))
    # Days before a cached pass/fail verdict is re-checked against the VK API.
    VERIFY_TTL_DAYS: int = int(_get("VERIFY_TTL_DAYS", "30"))

    # --- Passenger waiting queue (requirement 4) ----------------------------
    # Seconds a queued passenger has to confirm «still actual?» before skipping.
    PASSENGER_POLL_TIMEOUT: int = int(_get("PASSENGER_POLL_TIMEOUT", "120"))

    # --- Support link (requirement 7) ---------------------------------------
    SUPPORT_LINK: str = _get("SUPPORT_LINK", "https://vk.me/club0")

    # --- Maps ---------------------------------------------------------------
    # Yandex Distance Matrix / Google Distance Matrix key.
    MAPS_API_KEY: str = _get("MAPS_API_KEY", "")
    # "yandex" or "google"
    MAPS_PROVIDER: str = _get("MAPS_PROVIDER", "yandex").lower()

    # --- Pricing defaults (overridable via settings table) ------------------
    BASE_FARE: float = float(_get("BASE_FARE", "80"))          # руб, посадка
    PRICE_PER_KM: float = float(_get("PRICE_PER_KM", "20"))    # руб/км
    MIN_FARE: float = float(_get("MIN_FARE", "100"))           # руб

    # --- Timers (seconds) ---------------------------------------------------
    # Free waiting window after the driver arrives (default 3 minutes).
    FREE_WAITING_MINUTES: int = int(_get("FREE_WAITING_MINUTES", "3"))
    PRICE_PER_WAITING_MINUTE: float = float(_get("PRICE_PER_WAITING_MINUTE", "10"))
    # Driver has this long to accept/decline an offer before auto-timeout.
    # Requirement 8: 90 seconds, after which the driver is moved to the tail of
    # the free queue (kept on the line) and the order goes to the next driver.
    DRIVER_ACCEPT_TIMEOUT: int = int(_get("DRIVER_ACCEPT_TIMEOUT", "90"))
    DRIVER_CHAT_TIMEOUT: int = int(_get("DRIVER_CHAT_TIMEOUT", "600"))
    DRIVER_CHAT_PEER_ID: int = int(_get("DRIVER_CHAT_PEER_ID", "0"))
    DRIVER_FALLBACK_CHAT_PEER_ID: int = int(_get("DRIVER_FALLBACK_CHAT_PEER_ID", "0"))
    # Passenger has this long to pay after arrival before penalty block.
    PENALTY_FINE: float = float(_get("PENALTY_FINE", "200"))

    # --- Telegram ------------------------------------------------------------
    TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_WEBHOOK_URL: str = _get("TELEGRAM_WEBHOOK_URL", "")
    TELEGRAM_WEBHOOK_SECRET: str = _get("TELEGRAM_WEBHOOK_SECRET", "")
    TELEGRAM_OUTBOX_WORKERS: int = max(1, int(_get("TELEGRAM_OUTBOX_WORKERS", "2")))

    # --- Logging ------------------------------------------------------------
    LOG_DIR: Path = BASE_DIR / "logs"
    LOG_LEVEL: str = _get("LOG_LEVEL", "INFO").upper()

    @classmethod
    def sqlalchemy_url(cls) -> str:
        """Normalize the URL so SQLAlchemy always uses the psycopg2 driver."""
        url = cls.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url


config = Config()
