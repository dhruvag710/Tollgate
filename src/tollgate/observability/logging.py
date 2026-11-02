"""JSON logs (SPEC Phase 1.7): one object per line, every line with the
request_id of the request it was logged during (null outside requests).

RequestIDMiddleware sets the request id in a context variable for the whole
request, so it reaches every line logged on the request's behalf: uvicorn's
access log, and background tasks the request starts (asyncio copies the
context into them). uvicorn's own loggers are routed through the same JSON
handler, at LOG_LEVEL.

Nothing logged here ever holds a prompt or a key: the gateway's own messages
carry ids, counts and exception type names, and the access log drops the query
string, the one part of a request line a client controls freely. Unlike SPEC
§10's LOG_PROMPTS, there is no switch to log prompts (CLAUDE.md forbids it).
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": _message(record),
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
        }
        if record.exc_info:
            line["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def _message(record: logging.LogRecord) -> str:
    if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
        client, method, full_path, http_version, status = record.args
        path = str(full_path).split("?", 1)[0]
        return f'{client} - "{method} {path} HTTP/{http_version}" {status}'
    return record.getMessage()


class _TollgateHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marks the handler installed here, so configuring twice adds one only once."""


def configure_logging(level: str) -> None:
    """JSON lines on stderr at `level`, for the gateway's loggers and uvicorn's.
    Handlers others installed (a test runner's) are left in place."""
    logging.captureWarnings(True)  # warnings become log lines too, not plain stderr text
    root = logging.getLogger()
    root.setLevel(level)
    handler = next((h for h in root.handlers if isinstance(h, _TollgateHandler)), None)
    if handler is None:
        handler = _TollgateHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    handler.setLevel(level)
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()  # uvicorn's plain-text handlers
        logger.setLevel(logging.NOTSET)  # LOG_LEVEL rules, not uvicorn's --log-level
        logger.propagate = True
