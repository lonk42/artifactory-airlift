import logging
import os
import sys

import structlog


def configure(level: str | None = None) -> None:
    lvl = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, lvl, logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, lvl, logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
