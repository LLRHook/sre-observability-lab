"""Runtime settings, all from the environment; every value has a lab default."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = field(
        default_factory=lambda: os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
    )
    opensearch_url: str = field(
        default_factory=lambda: os.environ.get("OPENSEARCH_URL", "http://opensearch:9200")
    )
    opensearch_index: str = field(
        default_factory=lambda: os.environ.get("OPENSEARCH_INDEX", "srelab-logs-*")
    )
    ticketing_url: str = field(
        default_factory=lambda: os.environ.get("TICKETING_URL", "http://ticketing-api:8000")
    )
    # Least privilege: this token only unlocks ticketing-api's /admin/chaos* endpoints.
    ticketing_admin_token: str = field(
        default_factory=lambda: os.environ.get("TICKETING_ADMIN_TOKEN", "")
    )
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("COPILOT_DATA_DIR", "/data"))
    )
    runbook_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("RUNBOOK_DIR", "/runbooks"))
    )
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    model: str = field(default_factory=lambda: os.environ.get("COPILOT_MODEL", "claude-opus-5-5"))
    # Hard cap on untrusted text (logs, alert annotations) handed to the summarizer.
    max_untrusted_chars: int = 6000

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "incidents.sqlite3"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def channel_path(self) -> Path:
        return self.data_dir / "incident-channel.log"

    @property
    def tickets_path(self) -> Path:
        return self.data_dir / "tickets.jsonl"
