#!/usr/bin/env python3
"""Generate the Grafana dashboards in grafana/dashboards/*.json.

Dashboards-as-code without a DSL dependency: a few helper functions produce the
Grafana JSON model. `--check` fails if the committed JSON is stale (CI).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
AM = {"type": "alertmanager", "uid": "alertmanager"}
OS = {"type": "grafana-opensearch-datasource", "uid": "opensearch"}

_ids = iter(range(1, 10_000))


def target(expr: str, legend: str = "", instant: bool = False) -> dict[str, Any]:
    t: dict[str, Any] = {
        "datasource": PROM,
        "expr": expr,
        "legendFormat": legend or "__auto",
        "refId": "A",
    }
    if instant:
        t["instant"] = True
        t["range"] = False
    return t


def panel(
    kind: str,
    title: str,
    targets: list[dict[str, Any]],
    x: int,
    y: int,
    w: int,
    h: int,
    **opts: Any,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": next(_ids),
        "type": kind,
        "title": title,
        "datasource": targets[0]["datasource"] if targets else PROM,
        "targets": [dict(t, refId=chr(65 + i)) for i, t in enumerate(targets)],
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": opts.pop("defaults", {}),
            "overrides": opts.pop("overrides", []),
        },
        "options": opts.pop("options", {}),
    }
    p.update(opts)
    return p


def stat(
    title: str,
    expr: str,
    x: int,
    y: int,
    w: int = 4,
    h: int = 4,
    unit: str = "",
    thresholds: list[tuple[str, float | None]] | None = None,
    decimals: int | None = None,
) -> dict[str, Any]:
    steps = [{"color": c, "value": v} for c, v in (thresholds or [("green", None)])]
    defaults: dict[str, Any] = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return panel(
        "stat",
        title,
        [target(expr, instant=True)],
        x,
        y,
        w,
        h,
        defaults=defaults,
        options={
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "background",
            "graphMode": "none",
        },
    )


def timeseries(
    title: str,
    targets: list[dict[str, Any]],
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    unit: str = "",
    thresholds: list[float] | None = None,
    log: bool = False,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": unit, "custom": {"lineWidth": 1, "fillOpacity": 8}}
    if log:
        defaults["custom"]["scaleDistribution"] = {"type": "log", "log": 10}
    if thresholds:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": "transparent", "value": None}]
            + [{"color": "red", "value": t} for t in thresholds],
        }
        defaults["custom"]["thresholdsStyle"] = {"mode": "line"}
    return panel(
        "timeseries",
        title,
        targets,
        x,
        y,
        w,
        h,
        defaults=defaults,
        options={
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi"},
        },
    )


def dashboard(
    uid: str,
    title: str,
    panels: list[dict[str, Any]],
    templating: list[dict[str, Any]] | None = None,
    refresh: str = "30s",
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "tags": ["srelab"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "refresh": refresh,
        "time": {"from": "now-1h", "to": "now"},
        "templating": {"list": templating or []},
        "panels": panels,
        "links": [
            {
                "title": "Prometheus alerts",
                "type": "link",
                "url": "http://localhost:9090/alerts",
                "targetBlank": True,
            },
            {
                "title": "Alertmanager",
                "type": "link",
                "url": "http://localhost:9093",
                "targetBlank": True,
            },
        ],
    }


def slo_overview() -> dict[str, Any]:
    slo_var = {
        "name": "slo",
        "label": "SLO",
        "type": "query",
        "datasource": PROM,
        "query": {"query": "label_values(slo:objective:ratio, slo)", "refId": "slo"},
        "definition": "label_values(slo:objective:ratio, slo)",
        "refresh": 2,
        "includeAll": False,
        "multi": False,
        "current": {"text": "ticketing-api-availability", "value": "ticketing-api-availability"},
    }
    s = 'slo="$slo"'
    panels = [
        stat("Objective", f"slo:objective:ratio{{{s}}} * 100", 0, 0, unit="percent", decimals=2),
        stat(
            "SLI (30d)",
            f"(1 - slo:sli_error:ratio_rate30d{{{s}}}) * 100",
            4,
            0,
            unit="percent",
            decimals=3,
            thresholds=[("red", None), ("green", 99.0)],
        ),
        stat(
            "Error budget remaining (30d)",
            f"slo:error_budget_remaining:ratio{{{s}}} * 100",
            8,
            0,
            unit="percent",
            decimals=1,
            thresholds=[("red", None), ("orange", 25), ("green", 50)],
        ),
        stat(
            "Burn rate 5m",
            f"slo:burn_rate:ratio_rate5m{{{s}}}",
            12,
            0,
            unit="none",
            decimals=1,
            thresholds=[("green", None), ("orange", 6), ("red", 14.4)],
        ),
        stat(
            "Burn rate 1h",
            f"slo:burn_rate:ratio_rate1h{{{s}}}",
            16,
            0,
            unit="none",
            decimals=1,
            thresholds=[("green", None), ("orange", 6), ("red", 14.4)],
        ),
        stat(
            "Burn rate 6h",
            f"slo:burn_rate:ratio_rate6h{{{s}}}",
            20,
            0,
            unit="none",
            decimals=1,
            thresholds=[("green", None), ("orange", 1), ("red", 6)],
        ),
        timeseries(
            "SLI error ratio by window",
            [target(f"slo:sli_error:ratio_rate{w}{{{s}}}", w) for w in ("5m", "30m", "1h", "6h")],
            0,
            4,
            unit="percentunit",
        ),
        timeseries(
            "Burn rate by window (page thresholds 14.4x / 6x)",
            [
                target(f"slo:burn_rate:ratio_rate{w}{{{s}}}", w)
                for w in ("5m", "30m", "1h", "6h", "1d", "3d")
            ],
            12,
            4,
            thresholds=[6, 14.4],
            log=True,
        ),
        panel(
            "alertlist",
            "Firing alerts",
            [],
            0,
            12,
            12,
            8,
            datasource=AM,
            options={
                "alertName": "",
                "dashboardAlerts": False,
                "groupBy": [],
                "groupMode": "default",
                "maxItems": 20,
                "sortOrder": 1,
                "stateFilter": {
                    "firing": True,
                    "pending": True,
                    "noData": False,
                    "normal": False,
                    "error": False,
                },
                "viewMode": "list",
                "datasource": "Alertmanager",
            },
        ),
        timeseries(
            "Prometheus ALERTS (1 = firing/pending)",
            [target('ALERTS{service="ticketing-api"}', "{{alertname}} {{alertstate}}")],
            12,
            12,
        ),
        timeseries(
            "Error budget remaining (30d)",
            [target(f"slo:error_budget_remaining:ratio{{{s}}}", "remaining")],
            0,
            20,
            24,
            6,
            unit="percentunit",
            thresholds=[0],
        ),
    ]
    return dashboard("slo-overview", "SLO overview", panels, [slo_var])


def golden_signals() -> dict[str, Any]:
    job = 'job="ticketing-api"'
    pub = 'route!~"/metrics|/admin.*|/healthz|/readyz"'
    panels = [
        stat(
            "Request rate",
            f"sum(rate(http_requests_total{{{job},{pub}}}[5m]))",
            0,
            0,
            unit="reqps",
            decimals=1,
        ),
        stat(
            "5xx ratio (5m)",
            f'sum(rate(http_requests_total{{{job},{pub},status=~"5.."}}[5m])) / sum(rate(http_requests_total{{{job},{pub}}}[5m]))',
            4,
            0,
            unit="percentunit",
            decimals=2,
            thresholds=[("green", None), ("orange", 0.001), ("red", 0.01)],
        ),
        stat(
            "p99 latency",
            f"histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{{job},{pub}}}[5m])))",
            8,
            0,
            unit="s",
            decimals=3,
            thresholds=[("green", None), ("orange", 0.25), ("red", 0.5)],
        ),
        stat("In flight", f"sum(http_requests_in_flight{{{job}}})", 12, 0, unit="none", decimals=0),
        stat(
            "Synthetic probe",
            'min(probe_success{job="blackbox"})',
            16,
            0,
            unit="none",
            decimals=0,
            thresholds=[("red", None), ("green", 1)],
        ),
        stat(
            "Chaos: error rate",
            f"max(chaos_error_rate{{{job}}})",
            20,
            0,
            unit="percentunit",
            decimals=2,
            thresholds=[("green", None), ("red", 0.001)],
        ),
        timeseries(
            "Traffic by route",
            [target(f"sum by (route) (rate(http_requests_total{{{job},{pub}}}[1m]))", "{{route}}")],
            0,
            4,
            unit="reqps",
        ),
        timeseries(
            "Errors by route and status (5xx)",
            [
                target(
                    f'sum by (route, status) (rate(http_requests_total{{{job},{pub},status=~"5.."}}[1m]))',
                    "{{route}} {{status}}",
                )
            ],
            12,
            4,
            unit="reqps",
        ),
        timeseries(
            "Latency quantiles",
            [
                target(
                    f"histogram_quantile({q}, sum by (le) (rate(http_request_duration_seconds_bucket{{{job},{pub}}}[1m])))",
                    f"p{int(q * 100)}",
                )
                for q in (0.5, 0.9, 0.99)
            ],
            0,
            12,
            unit="s",
            thresholds=[0.5],
        ),
        timeseries(
            "Saturation: in-flight requests and client-side errors",
            [
                target(f"sum(http_requests_in_flight{{{job}}})", "in flight"),
                target(
                    'sum by (outcome) (rate(loadgen_requests_total{outcome!="ok"}[1m]))',
                    "loadgen {{outcome}}",
                ),
            ],
            12,
            12,
        ),
        timeseries(
            "Chaos state",
            [
                target(f"max(chaos_error_rate{{{job}}})", "error_rate"),
                target(f"max(chaos_latency_ms{{{job}}}) / 1000", "latency_s"),
                target(f"max(chaos_db_fault{{{job}}})", "db_fault"),
            ],
            0,
            20,
            12,
            6,
        ),
        timeseries(
            "Probe duration (blackbox)",
            [target('probe_duration_seconds{job="blackbox"}', "{{instance}}")],
            12,
            20,
            12,
            6,
            unit="s",
        ),
        panel(
            "logs",
            "Recent error/warn logs (OpenSearch)",
            [
                {
                    "datasource": OS,
                    "refId": "A",
                    "queryType": "lucene",
                    "query": "level:(ERROR OR WARNING)",
                    "metrics": [{"type": "logs", "id": "1"}],
                    "bucketAggs": [],
                    "timeField": "@timestamp",
                }
            ],
            0,
            26,
            24,
            10,
            options={
                "showTime": True,
                "wrapLogMessage": True,
                "sortOrder": "Descending",
                "enableLogDetails": True,
                "dedupStrategy": "none",
            },
        ),
    ]
    return dashboard("golden-signals", "Service golden signals", panels)


def postgres() -> dict[str, Any]:
    panels = [
        stat(
            "pg_up",
            "max(pg_up)",
            0,
            0,
            unit="none",
            decimals=0,
            thresholds=[("red", None), ("green", 1)],
        ),
        stat(
            "Connections",
            'sum(pg_stat_database_numbackends{datname="tickets"})',
            4,
            0,
            unit="none",
            decimals=0,
        ),
        stat(
            "Transactions/s",
            'sum(rate(pg_stat_database_xact_commit{datname="tickets"}[5m]) + rate(pg_stat_database_xact_rollback{datname="tickets"}[5m]))',
            8,
            0,
            unit="ops",
            decimals=1,
        ),
        stat(
            "Deadlocks (1h)",
            'sum(increase(pg_stat_database_deadlocks{datname="tickets"}[1h]))',
            12,
            0,
            unit="none",
            decimals=0,
            thresholds=[("green", None), ("red", 1)],
        ),
        stat(
            "DB fault injected",
            'max(chaos_db_fault{job="ticketing-api"})',
            16,
            0,
            unit="none",
            decimals=0,
            thresholds=[("green", None), ("red", 1)],
        ),
        stat(
            "Purchases/s (db_error)",
            'sum(rate(ticket_purchases_total{outcome="db_error"}[5m]))',
            20,
            0,
            unit="ops",
            decimals=2,
            thresholds=[("green", None), ("red", 0.01)],
        ),
        timeseries(
            "Commits vs rollbacks",
            [
                target('sum(rate(pg_stat_database_xact_commit{datname="tickets"}[1m]))', "commit"),
                target(
                    'sum(rate(pg_stat_database_xact_rollback{datname="tickets"}[1m]))', "rollback"
                ),
            ],
            0,
            4,
            unit="ops",
        ),
        timeseries(
            "Backends by state",
            [target('sum by (state) (pg_stat_activity_count{datname="tickets"})', "{{state}}")],
            12,
            4,
        ),
        timeseries(
            "Rows: inserted / fetched",
            [
                target(
                    'sum(rate(pg_stat_database_tup_inserted{datname="tickets"}[1m]))', "inserted"
                ),
                target('sum(rate(pg_stat_database_tup_fetched{datname="tickets"}[1m]))', "fetched"),
            ],
            0,
            12,
            unit="ops",
        ),
        timeseries(
            "App view: purchases by outcome",
            [target("sum by (outcome) (rate(ticket_purchases_total[1m]))", "{{outcome}}")],
            12,
            12,
            unit="ops",
        ),
        timeseries(
            "Correlation: app 5xx vs DB rollbacks",
            [
                target(
                    'sum(rate(http_requests_total{job="ticketing-api",status=~"5.."}[1m]))',
                    "app 5xx/s",
                ),
                target(
                    'sum(rate(pg_stat_database_xact_rollback{datname="tickets"}[1m]))',
                    "db rollbacks/s",
                ),
            ],
            0,
            20,
            24,
            7,
            unit="ops",
        ),
    ]
    return dashboard("postgres", "Postgres", panels)


DASHBOARDS = {
    "slo-overview.json": slo_overview,
    "golden-signals.json": golden_signals,
    "postgres.json": postgres,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for name, build in DASHBOARDS.items():
        global _ids
        _ids = iter(range(1, 10_000))
        text = json.dumps(build(), indent=2) + "\n"
        path = OUT_DIR / name
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)
            print(f"wrote {path}")
    if stale:
        print(f"stale dashboards: {stale}; run `make dashboards`", file=sys.stderr)
        return 1
    if args.check:
        print("dashboards up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
