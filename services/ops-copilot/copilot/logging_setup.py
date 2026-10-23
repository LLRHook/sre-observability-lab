"""JSON logs for the copilot, same shape as ticketing-api so one index template fits both."""

from __future__ import annotations

import json
import logging
import sys
import time

_STANDARD_ATTRS = set(logging.LogRecord("x", 0, "x", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI duplicate of msg
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "service": "ops-copilot",
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error", "httpx"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
