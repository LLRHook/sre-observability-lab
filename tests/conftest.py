"""Shared fakes for the copilot tests: no network, no LLM, no Docker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from copilot.audit import AuditLog, IncidentChannel
from copilot.models import AmAlert, AmWebhook, ProposedAction
from copilot.pipeline import Pipeline
from copilot.store import IncidentStore
from copilot.summarizer import RulesSummarizer


class FakePrometheus:
    """Answers PromQL by substring match against a table of canned results."""

    def __init__(self, values: dict[str, float] | None = None, routes: list[dict] | None = None):
        self.values = values or {}
        self.routes = routes or []
        self.queries: list[str] = []

    def query(self, promql: str) -> list[dict[str, Any]]:
        self.queries.append(promql)
        if "sum by (route, status)" in promql:
            return [
                {
                    "metric": {"route": r["route"], "status": r["status"]},
                    "value": [0, str(r["rps"])],
                }
                for r in self.routes
            ]
        for key, value in self.values.items():
            if key in promql:
                return [{"metric": {}, "value": [0, str(value)]}]
        return []


class FakeOpenSearch:
    def __init__(self, docs: list[dict[str, Any]] | None = None, fail: bool = False):
        self.docs = docs or []
        self.fail = fail
        self.bodies: list[dict] = []

    def search(self, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        if self.fail:
            raise ConnectionError("opensearch down")
        return {
            "hits": {
                "total": {"value": len(self.docs)},
                "hits": [{"_source": d} for d in self.docs],
            }
        }


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[ProposedAction, bool]] = []

    def execute(self, action: ProposedAction, dry_run: bool) -> dict[str, Any]:
        self.calls.append((action, dry_run))
        return {"dry_run": dry_run, "plan": {"action": action.action}, "result": {"ok": True}}


def make_alert(
    name: str = "TicketingApiAvailabilityBurnRatePage",
    severity: str = "page",
    status: str = "firing",
    fingerprint: str = "abc123",
    **extra_labels: str,
) -> AmAlert:
    labels = {
        "alertname": name,
        "severity": severity,
        "service": "ticketing-api",
        "slo": "ticketing-api-availability",
        "team": "platform-sre",
        **extra_labels,
    }
    return AmAlert(
        status=status,
        labels=labels,
        annotations={
            "summary": f"{severity}: {name}",
            "description": "test alert",
            "runbook_url": "http://localhost:8001/runbooks/ticketing-api-availability.md",
            "dashboard_url": "http://localhost:3000/d/slo-overview",
        },
        startsAt="2026-10-23T10:00:00Z",
        endsAt="0001-01-01T00:00:00Z" if status == "firing" else "2026-10-23T10:20:00Z",
        fingerprint=fingerprint,
    )


def make_webhook(*alerts: AmAlert, receiver: str = "pager") -> AmWebhook:
    return AmWebhook(
        version="4",
        groupKey='{}:{alertname="x"}',
        status="firing" if any(a.status == "firing" for a in alerts) else "resolved",
        receiver=receiver,
        alerts=list(alerts),
    )


@pytest.fixture
def runbook_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "docs" / "runbooks"


@pytest.fixture
def lab(tmp_path: Path, runbook_dir: Path):
    """A fully wired pipeline with fakes. Returns a namespace of parts."""

    class Parts:
        prom = FakePrometheus(
            values={"chaos_error_rate": 0.5, "burn_rate_5m": 500, "pg_up": 1, "up{": 1},
            routes=[{"route": "/purchase", "status": "500", "rps": 2.5}],
        )
        logs = FakeOpenSearch(
            docs=[{"level": "ERROR", "msg": "injected failure", "service": "ticketing-api"}] * 3
        )
        executor = FakeExecutor()
        store = IncidentStore(tmp_path / "incidents.sqlite3")
        audit = AuditLog(tmp_path / "audit.jsonl")
        channel = IncidentChannel(tmp_path / "channel.log")
        audit_path = tmp_path / "audit.jsonl"
        channel_path = tmp_path / "channel.log"

    parts = Parts()
    parts.pipeline = Pipeline(
        store=parts.store,
        audit=parts.audit,
        channel=parts.channel,
        summarizer=RulesSummarizer(),
        executor=parts.executor,
        metrics=parts.prom,
        logs=parts.logs,
        runbook_dir=runbook_dir,
    )
    return parts
