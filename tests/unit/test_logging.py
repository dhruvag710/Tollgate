import json
import logging
import os
import sys

from tollgate.observability.logging import (
    UVICORN_LOGGERS,
    JsonFormatter,
    configure_logging,
    request_id_var,
)


def record(
    name: str = "tollgate.x", msg: str = "hello %s", args: object = ("world",)
) -> logging.LogRecord:
    return logging.LogRecord(name, logging.WARNING, __file__, 1, msg, args, None)  # type: ignore[arg-type]


def test_a_line_is_one_json_object() -> None:
    line = JsonFormatter().format(record())
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["message"] == "hello world"
    assert (parsed["level"], parsed["logger"], parsed["request_id"]) == (
        "WARNING",
        "tollgate.x",
        None,
    )
    assert parsed["ts"].endswith("+00:00")


def test_the_request_id_comes_from_the_request_being_served() -> None:
    token = request_id_var.set("req-1")
    try:
        assert json.loads(JsonFormatter().format(record()))["request_id"] == "req-1"
    finally:
        request_id_var.reset(token)


def test_tracebacks_stay_on_the_line() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        r = record()
        r.exc_info = sys.exc_info()
    parsed = json.loads(JsonFormatter().format(r))
    assert "ValueError: boom" in parsed["exc_info"]


def test_the_access_log_drops_query_strings() -> None:
    access = record(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", "/v1/chat/completions?api_key=tg_live_secret", "1.1", 200),
    )
    message = json.loads(JsonFormatter().format(access))["message"]
    assert message == '127.0.0.1:5000 - "POST /v1/chat/completions HTTP/1.1" 200'


def test_configuring_twice_installs_one_handler_and_routes_uvicorn_through_it() -> None:
    try:
        configure_logging("INFO")
        configure_logging("DEBUG")
        ours = [h for h in logging.getLogger().handlers if isinstance(h.formatter, JsonFormatter)]
        assert len(ours) == 1
        assert ours[0].level == logging.DEBUG
        for name in UVICORN_LOGGERS:
            logger = logging.getLogger(name)
            assert (logger.handlers, logger.propagate, logger.level) == ([], True, logging.NOTSET)
    finally:
        # Back to the suite's level, not DEBUG for every test that follows.
        configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))
