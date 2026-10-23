"""Append-only JSONL audit log and the incident-channel sink.

Every step of every incident lands here: who/what/when, plus model and prompt
hash for LLM calls. Nothing is ever rewritten; the file is opened in append mode
on every write so a crash mid-run cannot truncate history.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("ops-copilot.audit")


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self, incident_id: str | None, step: str, actor: str, details: dict[str, Any] | None = None
    ) -> dict:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "incident_id": incident_id,
            "step": step,
            "actor": actor,
            "details": details or {},
        }
        line = json.dumps(entry, default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        log.info(
            "audit",
            extra={"event": "audit", "step": step, "incident_id": incident_id, "actor": actor},
        )
        return entry

    def for_incident(self, incident_id: str) -> list[dict]:
        if not self._path.exists():
            return []
        out = []
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("incident_id") == incident_id:
                    out.append(entry)
        return out


class IncidentChannel:
    """Stand-in for a Slack/Teams incident channel: a local append-only text file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def post(self, text: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n---- {stamp} ----\n{text.rstrip()}\n")
        log.info("incident channel post", extra={"event": "channel_post", "chars": len(text)})
