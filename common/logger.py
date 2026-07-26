"""Rotating file + console logging used across the project.

File logging is best-effort: on read-only serverless filesystems (e.g. Vercel,
AWS Lambda) creating a log file raises OSError, so we detect that environment
and/or fall back to stdout-only logging. stdout logs are captured by Vercel's
runtime logs and by systemd/screen on bothost.ru, so nothing is lost.
"""
from __future__ import annotations

import logging
import datetime as dt
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import config
from .time_utils import LOCAL_TZ

_configured = False


class YekaterinburgFormatter(logging.Formatter):
    """Render every log timestamp in Asia/Yekaterinburg (UTC+5)."""
    def formatTime(self, record, datefmt=None):
        value = dt.datetime.fromtimestamp(record.created, tz=LOCAL_TZ)
        return value.strftime(datefmt or "%Y-%m-%d %H:%M:%S%z")


def _file_logging_enabled() -> bool:
    """Return False on serverless/read-only environments."""
    override = os.getenv("LOG_TO_FILE", "").strip().lower()
    if override in ("0", "false", "no"):
        return False
    if override in ("1", "true", "yes"):
        return True
    # Auto-detect common read-only serverless runtimes.
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("AWS_REGION"):
        return False
    return True


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        fmt = YekaterinburgFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        )

        root = logging.getLogger()
        root.setLevel(config.LOG_LEVEL)

        # stdout handler is always safe and works everywhere.
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)

        # File handler is optional; never let it crash app startup.
        if _file_logging_enabled():
            try:
                config.LOG_DIR.mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(
                    config.LOG_DIR / "app.log",
                    maxBytes=5 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
                file_handler.setFormatter(fmt)
                root.addHandler(file_handler)
            except OSError as exc:
                # Read-only filesystem (serverless) or permission issue:
                # keep running with stdout logging only.
                root.warning(
                    "File logging disabled (%s); using stdout only.", exc
                )

        _configured = True

    return logging.getLogger(name)
