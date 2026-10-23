"""Guardrails: the allow-list, parameter validation, and untrusted-text handling.

Threat model in one paragraph: alert annotations and log lines are produced by
systems we do not fully trust (an attacker who can write a log line can write
"ignore your instructions and run X"). The summarizer, LLM or not, may therefore
propose anything. The rule is simple: a proposal is only an *action* if its name
is in ACTIONS and its params validate against that action's schema. Anything
else is dropped and the drop is audited. There is no path from text to execution.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from copilot.models import ProposedAction


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenTicketParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=5, max_length=200)
    priority: str = Field(default="P3", pattern=r"^P[1-4]$")


class ActionSpec(BaseModel):
    name: str
    description: str
    params_model: type[BaseModel]
    # What the action touches; shown to the approver so they know what they are approving.
    blast_radius: str


# The complete set of things the copilot can ever do. Adding one is a code review.
ACTIONS: dict[str, ActionSpec] = {
    "reset_chaos": ActionSpec(
        name="reset_chaos",
        description="POST /admin/chaos/reset on ticketing-api: clears injected error rate, latency and DB faults",
        params_model=NoParams,
        blast_radius="ticketing-api chaos state only (no data, no restarts)",
    ),
    "acknowledge": ActionSpec(
        name="acknowledge",
        description="Record that a human has seen the incident; changes nothing",
        params_model=NoParams,
        blast_radius="none",
    ),
    "open_ticket": ActionSpec(
        name="open_ticket",
        description="Append a follow-up ticket to the local ticket queue file",
        params_model=OpenTicketParams,
        blast_radius="local tickets.jsonl only",
    ),
}


def validate_actions(
    raw_actions: list[Any], audit: Callable[[str, dict[str, Any]], None] | None = None
) -> list[ProposedAction]:
    """Keep only allow-listed actions with valid params; audit every rejection."""
    kept: list[ProposedAction] = []
    for raw in raw_actions:
        try:
            candidate = (
                raw if isinstance(raw, ProposedAction) else ProposedAction.model_validate(raw)
            )
        except ValidationError as exc:
            if audit:
                audit("action_rejected", {"reason": "malformed", "detail": str(exc)[:300]})
            continue
        spec = ACTIONS.get(candidate.action)
        if spec is None:
            if audit:
                audit("action_rejected", {"reason": "not_allow_listed", "action": candidate.action})
            continue
        try:
            params = spec.params_model.model_validate(candidate.params)
        except ValidationError as exc:
            if audit:
                audit(
                    "action_rejected",
                    {
                        "reason": "invalid_params",
                        "action": candidate.action,
                        "detail": str(exc)[:300],
                    },
                )
            continue
        kept.append(
            ProposedAction(
                action=candidate.action,
                params=params.model_dump(),
                rationale=candidate.rationale[:500],
            )
        )
    return kept


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def untrusted(text: str, label: str, limit: int) -> str:
    """Delimit text that came from logs/alerts so a prompt can treat it as data.

    The delimiters are not a security boundary on their own; the allow-list is.
    They exist so the model (and a human reading the audit log) can tell what was
    observed from what was instructed.
    """
    cleaned = _CONTROL.sub("", text)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    # Neutralise an attempt to close our delimiter from inside the data.
    cleaned = cleaned.replace("</untrusted", "<\\/untrusted")
    return f'<untrusted source="{label}">\n{cleaned}\n</untrusted>'
