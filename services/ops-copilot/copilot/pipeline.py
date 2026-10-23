"""The copilot pipeline: webhook -> dedupe -> context -> triage -> proposal -> (human) -> execute.

Everything side-effecting is injected (store, audit, channel, executor, sources,
summarizer) so tests can run the whole flow with fakes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copilot.audit import AuditLog, IncidentChannel
from copilot.context import LogsSource, MetricsSource, gather_context
from copilot.executor import Executor
from copilot.guardrails import ACTIONS
from copilot.models import AmAlert, AmWebhook, Incident, IncidentStatus, TriageNote
from copilot.store import IncidentStore
from copilot.summarizer import Summarizer

log = logging.getLogger("ops-copilot.pipeline")


class ApprovalError(Exception):
    pass


class Pipeline:
    def __init__(
        self,
        store: IncidentStore,
        audit: AuditLog,
        channel: IncidentChannel,
        summarizer: Summarizer,
        executor: Executor,
        metrics: MetricsSource,
        logs: LogsSource,
        runbook_dir: Path,
    ) -> None:
        self.store = store
        self.audit = audit
        self.channel = channel
        self.summarizer = summarizer
        self.executor = executor
        self.metrics = metrics
        self.logs = logs
        self.runbook_dir = runbook_dir

    # ------------------------------------------------------------------ webhook
    def handle_webhook(self, payload: AmWebhook) -> dict[str, Any]:
        created, deduped, resolved = [], [], []
        for alert in payload.alerts:
            if alert.status == "resolved":
                incident = self.store.resolve(alert.fingerprint, alert.endsAt)
                if incident:
                    resolved.append(incident.id)
                    self.audit.record(
                        incident.id, "resolved", "alertmanager", {"ends_at": alert.endsAt}
                    )
                    self.channel.post(
                        f"RESOLVED {incident.alertname} ({incident.id}) at {alert.endsAt}"
                    )
                continue
            existing = self.store.find_active(alert.fingerprint)
            if existing:
                deduped.append(existing.id)
                self.audit.record(
                    existing.id, "dedupe", "alertmanager", {"receiver": payload.receiver}
                )
                continue
            incident = self._open_incident(alert, payload.receiver)
            created.append(incident.id)
        return {"created": created, "deduped": deduped, "resolved": resolved}

    def _open_incident(self, alert: AmAlert, receiver: str) -> Incident:
        incident = self.store.create(alert, receiver)
        self.audit.record(
            incident.id,
            "alert_received",
            "alertmanager",
            {
                "receiver": receiver,
                "alertname": alert.name,
                "fingerprint": alert.fingerprint,
                "labels": alert.labels,
            },
        )

        def audit_fn(step: str, details: dict[str, Any]) -> None:
            self.audit.record(incident.id, step, "copilot", details)

        context = gather_context(alert, self.metrics, self.logs, self.runbook_dir)
        audit_fn(
            "context_gathered",
            {
                "metric_keys": sorted(k for k in context["metrics"] if k != "errors"),
                "metric_errors": context["metrics"].get("errors", []),
                "log_hits": context["logs"].get("total", 0),
                "runbook_found": context["runbook"].get("found", False),
            },
        )
        triage = self.summarizer.summarize(alert, context, audit_fn)
        self.store.set_triage(incident.id, triage, context)
        audit_fn(
            "triage_note",
            {
                "summarizer": triage.summarizer,
                "classification": triage.classification.value,
                "confidence": triage.confidence,
                "proposed_actions": [a.action for a in triage.proposed_actions],
            },
        )
        self.channel.post(render_note(incident.id, alert, triage))
        return self.store.get(incident.id) or incident

    # ------------------------------------------------------------------ approval
    def approve(
        self, incident_id: str, approver: str, action_index: int, dry_run: bool
    ) -> dict[str, Any]:
        incident = self.store.get(incident_id)
        if incident is None:
            raise ApprovalError("no such incident")
        if not approver or not approver.strip():
            raise ApprovalError("approver identity is required")
        if incident.triage is None:
            raise ApprovalError("incident has no triage note yet")
        if action_index < 0 or action_index >= len(incident.triage.proposed_actions):
            raise ApprovalError("action index out of range")
        action = incident.triage.proposed_actions[action_index]
        if (
            action.action not in ACTIONS
        ):  # pragma: no cover - impossible after validation, kept on purpose
            raise ApprovalError("action is not allow-listed")

        self.audit.record(
            incident_id,
            "approval_granted",
            approver,
            {
                "action": action.action,
                "params": action.params,
                "dry_run": dry_run,
                "action_index": action_index,
            },
        )
        try:
            outcome = self.executor.execute(action, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            self.audit.record(
                incident_id,
                "execution_failed",
                "copilot",
                {"action": action.action, "error": str(exc)[:300]},
            )
            raise ApprovalError(f"execution failed: {exc}") from exc
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "approver": approver,
            "action": action.action,
            "params": action.params,
            "dry_run": dry_run,
            "outcome": outcome,
        }
        status = IncidentStatus.TRIAGED if dry_run else IncidentStatus.EXECUTED
        self.store.add_approval(incident_id, record, status)
        self.audit.record(
            incident_id,
            "executed" if not dry_run else "dry_run",
            "copilot",
            {"action": action.action, "outcome": outcome},
        )
        self.channel.post(
            f"{'DRY-RUN' if dry_run else 'EXECUTED'} {action.action} on {incident_id} approved by {approver}: {outcome.get('result')}"
        )
        return record

    def reject(self, incident_id: str, approver: str, reason: str) -> dict[str, Any]:
        incident = self.store.get(incident_id)
        if incident is None:
            raise ApprovalError("no such incident")
        if not approver.strip():
            raise ApprovalError("approver identity is required")
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "approver": approver,
            "rejected": True,
            "reason": reason[:300],
        }
        self.store.add_approval(incident_id, record, IncidentStatus.REJECTED)
        self.audit.record(incident_id, "approval_rejected", approver, {"reason": reason[:300]})
        self.channel.post(f"REJECTED proposals on {incident_id} by {approver}: {reason}")
        return record


def render_note(incident_id: str, alert: AmAlert, note: TriageNote) -> str:
    lines = [
        f"TRIAGE {incident_id}  [{note.summarizer}]  {alert.name}  severity={alert.labels.get('severity', '?')}",
        f"classification: {note.classification.value}  confidence: {note.confidence:.2f}",
        f"summary: {note.summary}",
        f"probable cause: {note.probable_cause}",
        f"severity assessment: {note.severity_assessment}",
        "evidence:",
        *[f"  - [{e.source}] {e.detail}" for e in note.evidence],
        "proposed actions (require approval: make approve INCIDENT=<id> ACTION=<n>):",
        *[
            f"  [{i}] {a.action} {a.params} - {a.rationale}"
            for i, a in enumerate(note.proposed_actions)
        ],
        f"runbook: {alert.annotations.get('runbook_url', '-')}",
        f"dashboard: {alert.annotations.get('dashboard_url', '-')}",
    ]
    return "\n".join(lines)


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()
