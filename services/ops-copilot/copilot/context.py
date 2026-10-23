"""Deterministic, read-only context gathering.

Everything here is a GET/POST-search against Prometheus and OpenSearch plus a
file read for the runbook. No side effects. The result is a plain dict so it can
be stored with the incident, shown to the approver, and hashed into the audit log.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

import httpx

from copilot.models import AmAlert

log = logging.getLogger("ops-copilot.context")

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]+$")


class MetricsSource(Protocol):
    def query(self, promql: str) -> list[dict[str, Any]]: ...


class LogsSource(Protocol):
    def search(self, body: dict[str, Any]) -> dict[str, Any]: ...


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def query(self, promql: str) -> list[dict[str, Any]]:
        resp = self._client.get("/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"prometheus error: {payload}")
        return payload["data"]["result"]


class OpenSearchClient:
    def __init__(self, base_url: str, index: str, timeout: float = 5.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._index = index

    def search(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(f"/{self._index}/_search", json=body)
        resp.raise_for_status()
        return resp.json()


def _scalar(result: list[dict[str, Any]]) -> float | None:
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _safe(value: str) -> str:
    """Only label values that are safe to splice into PromQL. Anything else is dropped."""
    return value if _SAFE_LABEL.match(value) else ""


def gather_metrics(prom: MetricsSource, alert: AmAlert) -> dict[str, Any]:
    service = _safe(alert.labels.get("service", "ticketing-api"))
    slo = _safe(alert.labels.get("slo", ""))
    out: dict[str, Any] = {"service": service, "slo": slo, "errors": []}

    def q(name: str, promql: str) -> None:
        try:
            out[name] = _scalar(prom.query(promql))
        except Exception as exc:  # noqa: BLE001 - context gathering must not break triage
            out["errors"].append(f"{name}: {exc.__class__.__name__}")
            out[name] = None

    if slo:
        for window in ("5m", "1h", "6h"):
            q(f"burn_rate_{window}", f'slo:burn_rate:ratio_rate{window}{{slo="{slo}"}}')
        q("sli_error_ratio_5m", f'slo:sli_error:ratio_rate5m{{slo="{slo}"}}')
        q("error_budget_remaining", f'slo:error_budget_remaining:ratio{{slo="{slo}"}}')
    q(
        "request_rate",
        f'sum(rate(http_requests_total{{job="{service}",route!~"/metrics|/admin.*|/healthz|/readyz"}}[5m]))',
    )
    q(
        "p99_latency_seconds",
        f'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{job="{service}"}}[5m])))',
    )
    q("pg_up", "max(pg_up)")
    q("chaos_error_rate", f'max(chaos_error_rate{{job="{service}"}})')
    q("chaos_latency_ms", f'max(chaos_latency_ms{{job="{service}"}})')
    q("chaos_db_fault", f'max(chaos_db_fault{{job="{service}"}})')
    q("target_up", f'max(up{{job="{service}"}})')

    try:
        rows = prom.query(
            f'sum by (route, status) (rate(http_requests_total{{job="{service}",status=~"5.."}}[5m]))'
        )
        out["errors_by_route"] = sorted(
            (
                {
                    "route": r["metric"].get("route", "?"),
                    "status": r["metric"].get("status", "?"),
                    "rps": round(float(r["value"][1]), 3),
                }
                for r in rows
            ),
            key=lambda r: -r["rps"],
        )[:10]
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"errors_by_route: {exc.__class__.__name__}")
        out["errors_by_route"] = []
    return out


def gather_logs(logs: LogsSource, alert: AmAlert, minutes: int = 15) -> dict[str, Any]:
    service = alert.labels.get("service", "ticketing-api")
    body = {
        "size": 50,
        "sort": [{"@timestamp": {"order": "desc"}}],
        "_source": [
            "@timestamp",
            "level",
            "msg",
            "event",
            "route",
            "status",
            "request_id",
            "service",
        ],
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": f"now-{int(minutes)}m"}}},
                    {"terms": {"level": ["ERROR", "WARNING", "CRITICAL"]}},
                    {"term": {"service": service}},
                ]
            }
        },
    }
    try:
        resp = logs.search(body)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}", "total": 0, "top_messages": [], "samples": []}
    hits = resp.get("hits", {}).get("hits", [])
    total = resp.get("hits", {}).get("total", {})
    total_n = total.get("value", len(hits)) if isinstance(total, dict) else total
    sources = [h.get("_source", {}) for h in hits]
    counter = Counter(str(s.get("msg", "")) for s in sources)
    return {
        "total": total_n,
        "window_minutes": minutes,
        "top_messages": [{"msg": m, "count": c} for m, c in counter.most_common(5)],
        "samples": sources[:8],
    }


def read_runbook(runbook_dir: Path, alert: AmAlert, limit: int = 4000) -> dict[str, Any]:
    url = alert.annotations.get("runbook_url", "")
    name = url.rsplit("/", 1)[-1] if url else ""
    if not re.fullmatch(r"[a-z0-9-]+\.md", name):
        return {"name": name, "found": False, "text": ""}
    path = runbook_dir / name
    if not path.is_file():
        return {"name": name, "found": False, "text": ""}
    text = path.read_text(encoding="utf-8")
    return {"name": name, "found": True, "text": text[:limit]}


def gather_context(
    alert: AmAlert, prom: MetricsSource, logs: LogsSource, runbook_dir: Path
) -> dict[str, Any]:
    return {
        "alert": {
            "name": alert.name,
            "labels": alert.labels,
            "annotations": alert.annotations,
            "starts_at": alert.startsAt,
        },
        "metrics": gather_metrics(prom, alert),
        "logs": gather_logs(logs, alert),
        "runbook": read_runbook(runbook_dir, alert),
    }
