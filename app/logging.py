"""Structured logging with PII redaction.

Transcripts belong in the database, not in log aggregation.
"""

import logging
import re
import sys

import structlog

from app.settings import get_settings

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# Phone-ish: 8-15 digits with optional separators, NOT adjacent to ':' or '-' in a
# way that would catch ISO timestamps, and not part of a longer alphanumeric token.
_PHONE = re.compile(r"(?<![\w:.\-/])(\+?\d[\d ().\-]{6,14}\d)(?![\w:.\-/])")

# Keys that are structural, never user content - redaction must not touch them.
_SKIP_KEYS = {
    "timestamp",
    "level",
    "event",
    "logger",
    "tenant_id",
    "conversation_id",
    "location_id",
    "job_id",
    "id",
    "latency_ms",
    "elapsed",
    "url",
    "start",
}


def _redact(_logger, _name, event_dict):
    for key, value in list(event_dict.items()):
        if key in _SKIP_KEYS or not isinstance(value, str):
            continue
        value = _EMAIL.sub("[email]", value)
        value = _PHONE.sub("[phone]", value)
        event_dict[key] = value
    return event_dict


def configure() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # httpx/hf chatter drowns real signal
    for noisy in ("httpx", "httpcore", "huggingface_hub", "filelock", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "engine"):
    return structlog.get_logger(name)
