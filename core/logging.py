"""Structured JSON logs, from commit one (STACK.md, Observability).

One JSON object per line on stdout. Extra keys passed to a logging call land as
top-level fields, so `log.info("synced", extra={"stream": "acls"})` is queryable
without parsing the message.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes the stdlib puts on every LogRecord. Anything outside this set was
# supplied by the caller via `extra=` and belongs in the JSON payload.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Formats records as single-line JSON."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info is not None:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(level: str = "INFO", service: str = "hippo") -> None:
    """Point the root logger at stdout with JSON formatting. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
