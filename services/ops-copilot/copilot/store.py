"""SQLite incident store. One row per incident, JSON columns for the nested bits."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from copilot.models import AmAlert, Incident, IncidentStatus, TriageNote

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    alertname TEXT NOT NULL,
    receiver TEXT NOT NULL,
    status TEXT NOT NULL,
    labels TEXT NOT NULL,
    annotations TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    triage TEXT,
    context TEXT NOT NULL DEFAULT '{}',
    approvals TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents(fingerprint, status);
"""


def _now() -> datetime:
    return datetime.now(UTC)


class IncidentStore:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)

    # -- helpers ----------------------------------------------------------------
    @staticmethod
    def _row_to_incident(row: sqlite3.Row) -> Incident:
        return Incident(
            id=row["id"],
            fingerprint=row["fingerprint"],
            alertname=row["alertname"],
            receiver=row["receiver"],
            status=IncidentStatus(row["status"]),
            labels=json.loads(row["labels"]),
            annotations=json.loads(row["annotations"]),
            starts_at=row["starts_at"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            triage=TriageNote.model_validate_json(row["triage"]) if row["triage"] else None,
            context=json.loads(row["context"]),
            approvals=json.loads(row["approvals"]),
        )

    # -- queries ------------------------------------------------------------------
    def find_active(self, fingerprint: str) -> Incident | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE fingerprint = ? AND status != ? "
                "ORDER BY created_at DESC LIMIT 1",
                (fingerprint, IncidentStatus.RESOLVED.value),
            ).fetchone()
        return self._row_to_incident(row) if row else None

    def get(self, incident_id: str) -> Incident | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        return self._row_to_incident(row) if row else None

    def list(self, limit: int = 50) -> list[Incident]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_incident(r) for r in rows]

    # -- mutations ------------------------------------------------------------------
    def create(self, alert: AmAlert, receiver: str) -> Incident:
        now = _now()
        incident = Incident(
            id=f"inc-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            fingerprint=alert.fingerprint,
            alertname=alert.name,
            receiver=receiver,
            status=IncidentStatus.OPEN,
            labels=alert.labels,
            annotations=alert.annotations,
            starts_at=alert.startsAt,
            created_at=now,
            updated_at=now,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO incidents (id, fingerprint, alertname, receiver, status, labels, "
                "annotations, starts_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    incident.id,
                    incident.fingerprint,
                    incident.alertname,
                    incident.receiver,
                    incident.status.value,
                    json.dumps(incident.labels),
                    json.dumps(incident.annotations),
                    incident.starts_at,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return incident

    def set_triage(self, incident_id: str, triage: TriageNote, context: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE incidents SET triage = ?, context = ?, status = ?, updated_at = ? WHERE id = ?",
                (
                    triage.model_dump_json(),
                    json.dumps(context, default=str),
                    IncidentStatus.TRIAGED.value,
                    _now().isoformat(),
                    incident_id,
                ),
            )

    def add_approval(self, incident_id: str, record: dict, status: IncidentStatus) -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT approvals FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            approvals = json.loads(row["approvals"]) if row else []
            approvals.append(record)
            self._conn.execute(
                "UPDATE incidents SET approvals = ?, status = ?, updated_at = ? WHERE id = ?",
                (json.dumps(approvals, default=str), status.value, _now().isoformat(), incident_id),
            )

    def resolve(self, fingerprint: str, ends_at: str) -> Incident | None:
        incident = self.find_active(fingerprint)
        if incident is None:
            return None
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE incidents SET status = ?, resolved_at = ?, updated_at = ? WHERE id = ?",
                (IncidentStatus.RESOLVED.value, now.isoformat(), now.isoformat(), incident.id),
            )
        return self.get(incident.id)
