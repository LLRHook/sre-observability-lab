#!/usr/bin/env python3
"""Generate Prometheus recording and alerting rules from slo/slos.yaml.

Usage:
    python tools/slo_gen.py                  # write prometheus/rules/generated/slo_rules.yml
    python tools/slo_gen.py --check          # exit 1 if the committed file is stale (CI)
    python tools/slo_gen.py --spec f --out g # explicit paths

Why generate instead of hand-write: one SLO needs ~7 windows x 2 recording rules
plus 2 alerts plus budget rules. Hand-maintaining that for every service is where
inconsistencies creep in (a window missing here, a wrong threshold there). The
spec is the thing humans edit; the rules are an artifact, checked in so promtool
can test them and reviewers can diff them.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SPEC = REPO / "slo" / "slos.yaml"
DEFAULT_OUT = REPO / "prometheus" / "rules" / "generated" / "slo_rules.yml"

_DURATION = re.compile(r"^(\d+)([smhdw])$")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def duration_seconds(text: str) -> int:
    m = _DURATION.match(str(text))
    if not m:
        raise ValueError(f"bad duration {text!r}; expected e.g. 5m, 1h, 30d")
    return int(m.group(1)) * _SECONDS[m.group(2)]


def validate_spec(spec: dict[str, Any]) -> None:
    if spec.get("apiVersion") != "lab/v1":
        raise ValueError("apiVersion must be lab/v1")
    names: set[str] = set()
    for slo in spec.get("slos", []):
        for key in ("name", "service", "objective", "sli"):
            if key not in slo:
                raise ValueError(f"slo missing {key!r}: {slo}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slo["name"]):
            raise ValueError(f"slo name must be lowercase kebab-case: {slo['name']!r}")
        if slo["name"] in names:
            raise ValueError(f"duplicate slo name {slo['name']!r}")
        names.add(slo["name"])
        if not 0 < float(slo["objective"]) < 100:
            raise ValueError(f"objective must be a percentage in (0, 100): {slo['objective']}")
        sli = slo["sli"]
        if sli.get("type") != "ratio":
            raise ValueError("only sli.type=ratio is supported")
        for key in ("errors", "total"):
            if "{{window}}" not in sli.get(key, ""):
                raise ValueError(f"sli.{key} must contain the [{{{{window}}}}] placeholder")
    alerting = merged_alerting(spec, {})
    for severity in ("page", "ticket"):
        for w in alerting[severity]["windows"]:
            if duration_seconds(w["long"]) <= duration_seconds(w["short"]):
                raise ValueError(f"long window must exceed short window: {w}")
            if float(w["burn_rate"]) <= 0:
                raise ValueError(f"burn_rate must be positive: {w}")


def merged_alerting(spec: dict[str, Any], slo: dict[str, Any]) -> dict[str, Any]:
    base = spec.get("defaults", {}).get("alerting", {})
    override = slo.get("alerting", {})
    out: dict[str, Any] = {}
    for severity in ("page", "ticket"):
        merged = dict(base.get(severity, {}))
        merged.update(override.get(severity, {}))
        if "windows" not in merged:
            raise ValueError(f"no {severity} windows defined")
        out[severity] = merged
    return out


def metric_name(slo_name: str) -> str:
    return slo_name.replace("-", "_")


def render(query: str, window: str) -> str:
    return " ".join(query.replace("{{window}}", window).split())


def recording_rules(spec: dict[str, Any], slo: dict[str, Any]) -> list[dict[str, Any]]:
    objective = float(slo["objective"]) / 100.0
    budget = 1.0 - objective
    slo_window = slo.get("window", spec.get("defaults", {}).get("window", "30d"))
    alerting = merged_alerting(spec, slo)

    def labels() -> dict[str, str]:
        # Fresh dict per rule so the YAML dumper never emits anchors/aliases.
        return {"slo": slo["name"], "service": slo["service"]}

    windows: list[str] = []
    for severity in ("page", "ticket"):
        for w in alerting[severity]["windows"]:
            for key in ("long", "short"):
                if w[key] not in windows:
                    windows.append(w[key])
    windows.sort(key=duration_seconds)
    if slo_window not in windows:
        windows.append(slo_window)

    rules: list[dict[str, Any]] = [
        {
            "record": "slo:objective:ratio",
            "expr": f"vector({objective:.6g})",
            "labels": labels(),
        },
        {
            "record": "slo:error_budget:ratio",
            "expr": f"vector({budget:.6g})",
            "labels": labels(),
        },
    ]
    for window in windows:
        errors = render(slo["sli"]["errors"], window)
        total = render(slo["sli"]["total"], window)
        # `or vector(0)`: when no error series exist yet (a brand-new service with
        # zero 5xx), sum() of an empty vector is empty, not 0, and the whole ratio
        # would vanish. A healthy SLI must read 0, not "no data".
        rules.append(
            {
                "record": f"slo:sli_error:ratio_rate{window}",
                "expr": f"(({errors}) or vector(0)) / ({total})",
                "labels": labels(),
            }
        )
        rules.append(
            {
                "record": f"slo:burn_rate:ratio_rate{window}",
                "expr": f'slo:sli_error:ratio_rate{window}{{slo="{slo["name"]}"}} / {budget:.6g}',
                "labels": labels(),
            }
        )
    rules.append(
        {
            "record": "slo:error_budget_remaining:ratio",
            "expr": (
                f'1 - (slo:sli_error:ratio_rate{slo_window}{{slo="{slo["name"]}"}} / {budget:.6g})'
            ),
            "labels": labels(),
        }
    )
    return rules


def alert_rules(spec: dict[str, Any], slo: dict[str, Any]) -> list[dict[str, Any]]:
    objective = float(slo["objective"]) / 100.0
    budget = 1.0 - objective
    name = slo["name"]
    alerting = merged_alerting(spec, slo)
    out: list[dict[str, Any]] = []
    for severity in ("page", "ticket"):
        cfg = alerting[severity]
        clauses = []
        for w in cfg["windows"]:
            br = float(w["burn_rate"])
            clauses.append(
                "(\n"
                f'    slo:burn_rate:ratio_rate{w["long"]}{{slo="{name}"}} > {br:g}\n'
                "    and\n"
                f'    slo:burn_rate:ratio_rate{w["short"]}{{slo="{name}"}} > {br:g}\n'
                "  )"
            )
        expr = "\n  or\n  ".join(clauses)
        windows_text = ", ".join(
            f"{w['burn_rate']}x over {w['long']}/{w['short']}" for w in cfg["windows"]
        )
        alert_name = "".join(part.capitalize() for part in name.split("-"))
        alert_name += "BurnRate" + ("Page" if severity == "page" else "Ticket")
        out.append(
            {
                "alert": alert_name,
                "expr": expr,
                "for": cfg.get("for", "0m"),
                "labels": {
                    "severity": severity,
                    "service": slo["service"],
                    "slo": name,
                    "team": slo.get("team", "platform-sre"),
                },
                "annotations": {
                    "summary": (
                        f"{severity.title()}: {name} is burning error budget ({windows_text})"
                    ),
                    "description": (
                        f"{slo.get('description', name)}. Objective {slo['objective']}% "
                        f"(error budget {budget * 100:.3g}% of requests over "
                        f"{slo.get('window', spec.get('defaults', {}).get('window', '30d'))}). "
                        f"Both windows of at least one pair ({windows_text}) exceed their "
                        "burn-rate threshold. Check per-window burn and recent errors on the "
                        "dashboard; the runbook lists the first diagnostic queries."
                    ),
                    "runbook_url": slo["runbook_url"],
                    "dashboard_url": slo["dashboard_url"],
                },
            }
        )
    return out


def generate(spec: dict[str, Any]) -> dict[str, Any]:
    validate_spec(spec)
    groups: list[dict[str, Any]] = []
    for slo in spec["slos"]:
        groups.append(
            {
                "name": f"slo-{slo['name']}-recording",
                "rules": recording_rules(spec, slo),
            }
        )
        groups.append(
            {
                "name": f"slo-{slo['name']}-alerts",
                "rules": alert_rules(spec, slo),
            }
        )
    return {"groups": groups}


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_presenter)


def to_yaml(rules: dict[str, Any], spec_path: Path) -> str:
    header = (
        "# GENERATED FILE - do not edit by hand.\n"
        f"# Source: {spec_path.as_posix()} via tools/slo_gen.py. Regenerate with `make rules`.\n"
        "---\n"
    )
    return header + yaml.dump(rules, Dumper=_Dumper, sort_keys=False, width=120)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="fail if output is stale")
    args = parser.parse_args(argv)

    spec = yaml.safe_load(args.spec.read_text())
    text = to_yaml(
        generate(spec), args.spec.relative_to(REPO) if args.spec.is_relative_to(REPO) else args.spec
    )
    if args.check:
        current = args.out.read_text() if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is stale; run `make rules`", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    n_rules = sum(len(g["rules"]) for g in generate(spec)["groups"])
    print(f"wrote {args.out} ({n_rules} rules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
