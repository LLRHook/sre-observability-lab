"""Executes *approved* allow-listed actions. This is the only module with write access
to anything outside the copilot's own data directory, and the only thing it can
reach is ticketing-api's admin endpoints with a scoped token."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from copilot.guardrails import ACTIONS
from copilot.models import ProposedAction


class Executor(Protocol):
    def execute(self, action: ProposedAction, dry_run: bool) -> dict[str, Any]: ...


class HttpExecutor:
    def __init__(
        self, ticketing_url: str, admin_token: str, tickets_path: Path, timeout: float = 5.0
    ) -> None:
        self._client = httpx.Client(base_url=ticketing_url, timeout=timeout)
        self._token = admin_token
        self._tickets_path = tickets_path

    def execute(self, action: ProposedAction, dry_run: bool) -> dict[str, Any]:
        spec = ACTIONS.get(action.action)
        if spec is None:
            # Belt and braces: the pipeline already filtered, but never trust the caller.
            raise PermissionError(f"action {action.action!r} is not allow-listed")
        spec.params_model.model_validate(action.params)
        plan = {"action": action.action, "params": action.params, "blast_radius": spec.blast_radius}
        if dry_run:
            return {"dry_run": True, "plan": plan, "result": None}

        if action.action == "reset_chaos":
            resp = self._client.post("/admin/chaos/reset", headers={"x-admin-token": self._token})
            resp.raise_for_status()
            return {"dry_run": False, "plan": plan, "result": resp.json()}
        if action.action == "acknowledge":
            return {"dry_run": False, "plan": plan, "result": {"acknowledged": True}}
        if action.action == "open_ticket":
            record = {
                "ts": datetime.now(UTC).isoformat(),
                "summary": action.params["summary"],
                "priority": action.params.get("priority", "P3"),
            }
            self._tickets_path.parent.mkdir(parents=True, exist_ok=True)
            with self._tickets_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            return {"dry_run": False, "plan": plan, "result": record}
        raise PermissionError(f"no executor branch for {action.action!r}")  # pragma: no cover
