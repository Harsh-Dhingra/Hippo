"""Structured logging: one JSON object per line, extras promoted to fields."""

import json
import logging
from collections.abc import Iterator

import pytest

from core.logging import JsonFormatter, configure_logging


@pytest.fixture
def formatter() -> JsonFormatter:
    return JsonFormatter(service="hippo-test")


def _record(**kwargs: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="hippo.example",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="synced %s",
        args=("acls",),
        exc_info=None,
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


def test_emits_valid_single_line_json(formatter: JsonFormatter) -> None:
    line = formatter.format(_record())
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["message"] == "synced acls"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "hippo.example"
    assert payload["service"] == "hippo-test"
    assert payload["ts"].endswith("+00:00")


def test_promotes_extra_keys_to_top_level_fields(formatter: JsonFormatter) -> None:
    payload = json.loads(formatter.format(_record(stream="acls", connector_id=7)))
    assert payload["stream"] == "acls"
    assert payload["connector_id"] == 7


def test_does_not_leak_standard_record_attributes(formatter: JsonFormatter) -> None:
    payload = json.loads(formatter.format(_record()))
    for noise in ("msg", "args", "pathname", "levelno", "relativeCreated"):
        assert noise not in payload


def test_serialises_unjsonable_extras_instead_of_raising(formatter: JsonFormatter) -> None:
    payload = json.loads(formatter.format(_record(target=object())))
    assert payload["target"].startswith("<object object")


def test_includes_exception_text() -> None:
    formatter = JsonFormatter(service="hippo-test")
    try:
        raise ValueError("inverse capture failed")
    except ValueError:
        record = _record()
        import sys

        record.exc_info = sys.exc_info()
        payload = json.loads(formatter.format(record))
    assert "ValueError: inverse capture failed" in payload["exception"]


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(saved)
    root.setLevel(saved_level)


def test_configure_logging_writes_json_to_stdout(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", service="hippo-api")
    logging.getLogger("hippo.example").info("ready", extra={"port": 8000})

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["message"] == "ready"
    assert payload["service"] == "hippo-api"
    assert payload["port"] == 8000


def test_configure_logging_is_idempotent(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", service="hippo-api")
    configure_logging(level="INFO", service="hippo-api")
    logging.getLogger("hippo.example").info("once")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1, "repeated configuration must not duplicate handlers"


def test_configure_logging_respects_level(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="WARNING", service="hippo-api")
    logging.getLogger("hippo.example").info("suppressed")
    logging.getLogger("hippo.example").warning("shown")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "shown"
