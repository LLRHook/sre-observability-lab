"""Pydantic schemas: what Alertmanager sends us, and what a triage note must look like.

The TriageNote schema is the contract between the summarizer (LLM or rules) and
everything downstream. Both summarizers must produce exactly this shape, which is
what makes the LLM swappable and the guardrails testable.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# --- Alertmanager webhook payload (v4) ----------------------------------------
class AmAlert(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str = ""
    endsAt: str = ""
    generatorURL: str = ""
    fingerprint: str

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "unknown")


class AmWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str = "4"
    groupKey: str = ""
    status: str
    receiver: str
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[AmAlert]


# --- Triage note ---------------------------------------------------------------
class Classification(StrEnum):
    ERROR_RATE_SPIKE = "error_rate_spike"
    LATENCY_DEGRADATION = "latency_degradation"
    DEPENDENCY_FAILURE = "dependency_failure"
    INJECTED_FAULT = "injected_fault"
    TARGET_UNREACHABLE = "target_unreachable"
    UNKNOWN = "unknown"


class ProposedAction(BaseModel):
    """An action the copilot *proposes*. Nothing here executes without approval."""

    model_config = ConfigDict(extra="forbid")
    action: str = Field(description="Must be one of the allow-listed action names")
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(max_length=500)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(description="prometheus | opensearch | runbook | alert")
    detail: str = Field(max_length=500)


class TriageNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: Classification
    severity_assessment: str = Field(max_length=300)
    summary: str = Field(max_length=1200)
    probable_cause: str = Field(max_length=600)
    evidence: list[Evidence] = Field(default_factory=list, max_length=12)
    proposed_actions: list[ProposedAction] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)
    summarizer: str = Field(default="rules", description="rules | llm | llm-fallback-rules")


class LLMTriage(BaseModel):
    """What we ask the model for. Deliberately a subset of TriageNote: the model
    never sets `summarizer`, and its actions are re-validated by guardrails."""

    model_config = ConfigDict(extra="forbid")
    classification: Classification
    severity_assessment: str = Field(max_length=300)
    summary: str = Field(max_length=1200)
    probable_cause: str = Field(max_length=600)
    evidence: list[Evidence] = Field(default_factory=list, max_length=12)
    proposed_actions: list[ProposedAction] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)


# --- Incident record -------------------------------------------------------------
class IncidentStatus(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"
    EXECUTED = "executed"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class Incident(BaseModel):
    id: str
    fingerprint: str
    alertname: str
    receiver: str
    status: IncidentStatus
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    triage: TriageNote | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
