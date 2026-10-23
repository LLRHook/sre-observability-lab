"""ops-copilot HTTP surface: Alertmanager webhook in, incidents + approvals out."""

from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from copilot.audit import AuditLog, IncidentChannel
from copilot.config import Settings
from copilot.context import OpenSearchClient, PrometheusClient
from copilot.executor import HttpExecutor
from copilot.logging_setup import configure_logging
from copilot.models import AmWebhook
from copilot.pipeline import ApprovalError, Pipeline
from copilot.store import IncidentStore
from copilot.summarizer import LLMSummarizer, RulesSummarizer

log = logging.getLogger("ops-copilot")

WEBHOOKS = Counter(
    "copilot_webhooks_total", "Alertmanager webhooks received", ["receiver", "status"]
)
INCIDENTS = Counter("copilot_incidents_total", "Incidents by outcome", ["outcome"])
TRIAGE_SECONDS = Histogram(
    "copilot_triage_duration_seconds", "Time to produce a triage note", ["summarizer"]
)
APPROVALS = Counter(
    "copilot_approvals_total", "Approval decisions", ["decision", "action", "dry_run"]
)


def build_pipeline(settings: Settings) -> Pipeline:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    rules = RulesSummarizer()
    summarizer = (
        LLMSummarizer(
            settings.anthropic_api_key, settings.model, settings.max_untrusted_chars, fallback=rules
        )
        if settings.llm_enabled
        else rules
    )
    return Pipeline(
        store=IncidentStore(settings.db_path),
        audit=AuditLog(settings.audit_path),
        channel=IncidentChannel(settings.channel_path),
        summarizer=summarizer,
        executor=HttpExecutor(
            settings.ticketing_url, settings.ticketing_admin_token, settings.tickets_path
        ),
        metrics=PrometheusClient(settings.prometheus_url),
        logs=OpenSearchClient(settings.opensearch_url, settings.opensearch_index),
        runbook_dir=settings.runbook_dir,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = Settings()
    app.state.settings = settings
    app.state.pipeline = build_pipeline(settings)
    log.info(
        "ops-copilot ready",
        extra={
            "event": "startup",
            "summarizer": app.state.pipeline.summarizer.name,
            "model": settings.model if settings.llm_enabled else None,
        },
    )
    yield


app = FastAPI(title="ops-copilot", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "summarizer": app.state.pipeline.summarizer.name}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/alertmanager/webhook/{receiver}")
def alertmanager_webhook(receiver: str, payload: AmWebhook) -> dict:
    pipeline: Pipeline = app.state.pipeline
    WEBHOOKS.labels(receiver, payload.status).inc()
    start = time.perf_counter()
    result = pipeline.handle_webhook(payload)
    for _ in result["created"]:
        INCIDENTS.labels("created").inc()
        TRIAGE_SECONDS.labels(pipeline.summarizer.name).observe(time.perf_counter() - start)
    for _ in result["deduped"]:
        INCIDENTS.labels("deduped").inc()
    for _ in result["resolved"]:
        INCIDENTS.labels("resolved").inc()
    log.info("webhook handled", extra={"event": "webhook", "receiver": receiver, **result})
    return result


@app.get("/incidents")
def list_incidents(limit: int = 50) -> list[dict]:
    pipeline: Pipeline = app.state.pipeline
    return [
        i.model_dump(mode="json", exclude={"context"})
        for i in pipeline.store.list(limit=min(limit, 200))
    ]


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    pipeline: Pipeline = app.state.pipeline
    incident = pipeline.store.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="no such incident")
    data = incident.model_dump(mode="json")
    data["audit"] = pipeline.audit.for_incident(incident_id)
    return data


class ApproveRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=120)
    action_index: int = Field(default=0, ge=0)
    dry_run: bool = False


class RejectRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=300)


@app.post("/incidents/{incident_id}/approve")
def approve(incident_id: str, body: ApproveRequest, request: Request) -> dict:
    pipeline: Pipeline = app.state.pipeline
    try:
        record = pipeline.approve(incident_id, body.approver, body.action_index, body.dry_run)
    except ApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    APPROVALS.labels("approved", record["action"], str(body.dry_run).lower()).inc()
    return record


@app.post("/incidents/{incident_id}/reject")
def reject(incident_id: str, body: RejectRequest) -> dict:
    pipeline: Pipeline = app.state.pipeline
    try:
        record = pipeline.reject(incident_id, body.approver, body.reason)
    except ApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    APPROVALS.labels("rejected", "-", "false").inc()
    return record


@app.get("/incidents/{incident_id}/audit")
def incident_audit(incident_id: str) -> list[dict]:
    pipeline: Pipeline = app.state.pipeline
    return pipeline.audit.for_incident(incident_id)


_RUNBOOK_NAME = re.compile(r"^[a-z0-9-]+\.md$")


@app.get("/runbooks/{name}", response_class=PlainTextResponse)
def runbook(name: str) -> str:
    """Serve docs/runbooks so runbook_url annotations resolve inside the lab."""
    if not _RUNBOOK_NAME.match(name):
        raise HTTPException(status_code=404)
    path: Path = app.state.settings.runbook_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return path.read_text(encoding="utf-8")
