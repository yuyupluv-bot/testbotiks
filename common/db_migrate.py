"""Programmatic Alembic upgrade.

Running ``alembic upgrade head`` automatically on process startup keeps the
database schema in sync with ``common/models.py`` on every deploy, so adding a
new column/model never requires a manual migration step on the server
(bothost.ru / Docker / VPS). It is safe and idempotent: Alembic only applies
revisions that have not been recorded yet, and each migration additionally
guards its own column additions.
"""
from __future__ import annotations

import os
import threading
import logging

from common.logger import get_logger

log = get_logger("common.db_migrate")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INI = os.path.join(_PROJECT_ROOT, "alembic.ini")
_SCRIPTS = os.path.join(_PROJECT_ROOT, "migrations")

_lock = threading.Lock()
_done = False


def ensure_schema() -> bool:
    """Apply any pending migrations. Returns True on success.

    Never raises: a migration failure is logged so the caller (bot long-poll
    loop / web app) can decide whether to keep running.
    """
    global _done
    with _lock:
        if _done:
            return True
        # IMPORTANT: migrations/base tables must be created BEFORE ALTER TABLE
        # guards. On a completely empty database the previous order made every
        # ALTER fail, created only a few standalone reliability tables, and
        # returned before Alembic could create users/orders/cities.
        try:
            if os.path.exists(_INI):
                from alembic import command
                from alembic.config import Config

                cfg = Config(_INI)
                # Use absolute paths so it works regardless of the current working
                # directory (systemd, Docker WORKDIR, `python -m bot.main`, etc.).
                cfg.set_main_option("script_location", _SCRIPTS)
                command.upgrade(cfg, "head")
                # Alembic's fileConfig changes the root level to WARN. Restore
                # the application level so BotHost exposes startup failures.
                from common.config import config as app_config
                logging.getLogger().setLevel(app_config.LOG_LEVEL)
                for logger_name in ("bot.main", "bot.vk", "bot.outbox", "bot.pqueue", "common.db_migrate"):
                    logging.getLogger(logger_name).disabled = False
            else:
                # Hosting fallback when alembic.ini was not included: create
                # the complete current model, not just standalone guard tables.
                from common.database import engine
                from common.models import Base

                Base.metadata.create_all(bind=engine)
                log.warning("alembic.ini is absent; Base metadata created")

            if not ensure_columns():
                log.error("Required raw schema guard failed after base migration")
                return False
            _done = True
            log.info("Database base and schema guards completed")
            print("STARTUP DATABASE OK: schema is ready", flush=True)
            return True
        except Exception:  # noqa: BLE001 - caller decides whether to stop
            # Include the original DB/Alembic traceback. Previously only the
            # outer RuntimeError was visible in some hosting logs, hiding the
            # actual migration failure.
            log.exception("Auto-migration (alembic upgrade head) failed")
            return False


def ensure_columns() -> bool:
    """Idempotent raw DDL guard so required columns always exist, even where
    Alembic/migrations are unavailable (Docker image at /app, serverless).
    Safe to run on every startup; never raises."""
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS passenger_rating_sum INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS passenger_rating_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'passenger_to_driver'",
        "ALTER TABLE drivers_queue ADD COLUMN IF NOT EXISTS front_notified BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS driver_missed_offers INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE blocked_users ADD COLUMN IF NOT EXISTS notice_sent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE fake_calls ADD COLUMN IF NOT EXISTS payment_requested_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_phone VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_bank TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS show_payment_details BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled_by VARCHAR(20)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS route_text TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS voice_attachment TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ride_waiting_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_details_sent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS chat_driver_was_offline BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_driver_id INTEGER REFERENCES users(id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_eta INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_eta_set_at TIMESTAMPTZ",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_notified_driver_ids TEXT",
        "CREATE INDEX IF NOT EXISTS ix_orders_parallel_driver_id ON orders(parallel_driver_id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS offered_driver_id INTEGER REFERENCES users(id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS offer_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS departure_prompt_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS actuality_confirmed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS chat_notice_outbox_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_orders_offered_driver_id ON orders(offered_driver_id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS declined_driver_ids TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS decline_reasons_json TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_decline_reason VARCHAR(40)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name VARCHAR(255)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(64)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS driver_departed_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_type VARCHAR(20)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_card VARCHAR(32)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_recipient VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_rules_sent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS driver_gender VARCHAR(10)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS away_notice_outbox_id INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS away_notice_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS temporary_driver_until TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_telegram_user_id ON users(telegram_user_id) WHERE telegram_user_id IS NOT NULL",
        """CREATE TABLE IF NOT EXISTS telegram_outbox_messages (
            id SERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, text TEXT,
            reply_markup TEXT, status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claimed_at TIMESTAMPTZ, sent_at TIMESTAMPTZ, last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS ix_telegram_outbox_pending ON telegram_outbox_messages(status, next_attempt_at, id)",
        """CREATE TABLE IF NOT EXISTS telegram_processed_events (
            id SERIAL PRIMARY KEY, update_id BIGINT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS ix_telegram_processed_created ON telegram_processed_events(created_at)",
        """CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            passenger_id INTEGER NOT NULL REFERENCES users(id),
            driver_id INTEGER REFERENCES users(id),
            order_id INTEGER UNIQUE REFERENCES orders(id),
            type VARCHAR(30) NOT NULL,
            scheduled_time TIME NOT NULL,
            scheduled_at TIMESTAMPTZ NOT NULL,
            from_address VARCHAR(500) NOT NULL,
            to_address VARCHAR(500) NOT NULL DEFAULT '',
            route_text TEXT NOT NULL,
            extra_services TEXT,
            comment TEXT NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            canceled_by VARCHAR(20),
            reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_bookings_passenger_id ON bookings(passenger_id)",
        "CREATE INDEX IF NOT EXISTS ix_bookings_driver_id ON bookings(driver_id)",
        "CREATE INDEX IF NOT EXISTS ix_bookings_status ON bookings(status)",
        "CREATE INDEX IF NOT EXISTS ix_bookings_scheduled_at ON bookings(scheduled_at)",
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS chat_notice_outbox_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_orders_passenger_status_created ON orders(passenger_id, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_orders_driver_status_created ON orders(driver_id, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_orders_dispatcher_status_created ON orders(dispatcher_id, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_driver_queue_status_position ON drivers_queue(status, position)",
        "CREATE INDEX IF NOT EXISTS ix_passenger_queue_status_position ON passenger_queue(status, position)",
        "CREATE INDEX IF NOT EXISTS ix_bookings_status_scheduled ON bookings(status, scheduled_at)",
        """CREATE TABLE IF NOT EXISTS outbox_messages (
            id SERIAL PRIMARY KEY, peer_id BIGINT NOT NULL, text TEXT,
            keyboard TEXT, attachment TEXT, random_id BIGINT NOT NULL UNIQUE,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claimed_at TIMESTAMPTZ, sent_at TIMESTAMPTZ, last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS processed_events (
            id SERIAL PRIMARY KEY, event_key VARCHAR(255) NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id SERIAL PRIMARY KEY, job_key VARCHAR(120) NOT NULL UNIQUE,
            kind VARCHAR(40) NOT NULL, object_id INTEGER NOT NULL,
            run_at TIMESTAMPTZ NOT NULL, payload TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS login_attempts (
            id SERIAL PRIMARY KEY, ip_address VARCHAR(64) NOT NULL,
            login VARCHAR(120) NOT NULL, success BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox_messages(status, next_attempt_at)",
        "ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_outbox_priority_pending ON outbox_messages(status, priority DESC, next_attempt_at, id)",
        "CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_due ON scheduled_jobs(status, run_at)",
        "CREATE INDEX IF NOT EXISTS ix_login_attempts_lookup ON login_attempts(ip_address, login, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_processed_events_created ON processed_events(created_at)",
        # Release 0141 removed the aggregate unclaimed-order chat notice.
        # Cancel every legacy queued/retrying row before the outbox workers
        # start, otherwise an old retry could still post after deployment.
        """UPDATE outbox_messages
            SET status = 'cancelled', claimed_at = NULL,
                last_error = 'removed aggregate notice (0141)'
            WHERE text LIKE '🚕 Есть %'
              AND status NOT IN ('cancelled')""",
        """DELETE FROM settings WHERE key IN (
            'unclaimed_notice_outbox_id', 'unclaimed_notice_count',
            'unclaimed_notice_peer_id', 'unclaimed_notice_cleanup_version'
        )""",
        "UPDATE settings SET value = '👥 Свободные водители' WHERE key = 'btn_drivers' AND value IN ('👥 Водители', 'Водители')",
    ]
    try:
        from sqlalchemy import text
        from common.database import engine
    except Exception as exc:  # noqa: BLE001
        log.error("ensure_columns: cannot import engine: %s", exc)
        return False
    ok = True
    for stmt in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.error("ensure_columns DDL failed [%s]: %s", stmt, exc)
    if ok:
        log.info("ensure_columns: schema columns verified")
    return ok
