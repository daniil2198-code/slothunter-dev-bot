"""structlog setup — JSON logs to stdout (systemd journal eats them as-is)."""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import settings


def configure_logging() -> None:
    """Idempotent — safe to call from main + workers."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        # filtering_bound_logger is a non-stdlib wrapper; pair it with
        # structlog.processors.* (NOT structlog.stdlib.*), otherwise
        # processors that expect PythonLogger attributes (like
        # add_logger_name → logger.name) crash on every log call.
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.types.FilteringBoundLogger:
    # ``name`` is included as a key in every log line so we keep some
    # module locality without relying on stdlib's logger hierarchy.
    return structlog.get_logger().bind(logger=name)  # type: ignore[no-any-return]
