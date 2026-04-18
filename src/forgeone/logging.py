"""Structlog JSON setup. Every log line carries `bucket` and `strategy_mode`."""
from __future__ import annotations

import logging
import sys

import structlog


def configure(level: str = "INFO", *, bucket: str | None = None, strategy_mode: str | None = None) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if bucket or strategy_mode:
        ctx = {}
        if bucket:
            ctx["bucket"] = bucket
        if strategy_mode:
            ctx["strategy_mode"] = strategy_mode
        structlog.contextvars.bind_contextvars(**ctx)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()
