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
import sys
from pathlib import Path
from typing import Any

import yaml

if __package__:
    from .slo_native import call as _native
else:
    from slo_native import call as _native

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SPEC = REPO / "slo" / "slos.yaml"
DEFAULT_OUT = REPO / "prometheus" / "rules" / "generated" / "slo_rules.yml"


def duration_seconds(text: str) -> int:
    return _native("duration", text)


def validate_spec(spec: dict[str, Any]) -> None:
    return _native("validate", spec)


def merged_alerting(spec: dict[str, Any], slo: dict[str, Any]) -> dict[str, Any]:
    return _native("merged", spec, slo)


def metric_name(slo_name: str) -> str:
    return _native("metric_name", slo_name)


def render(query: str, window: str) -> str:
    return _native("render", query, window)


def recording_rules(spec: dict[str, Any], slo: dict[str, Any]) -> list[dict[str, Any]]:
    return _native("recording", spec, slo)


def alert_rules(spec: dict[str, Any], slo: dict[str, Any]) -> list[dict[str, Any]]:
    return _native("alerts", spec, slo)


def generate(spec: dict[str, Any]) -> dict[str, Any]:
    return _native("generate", spec)


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
    rules = generate(spec)
    text = to_yaml(
        rules, args.spec.relative_to(REPO) if args.spec.is_relative_to(REPO) else args.spec
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
    n_rules = sum(len(g["rules"]) for g in rules["groups"])
    print(f"wrote {args.out} ({n_rules} rules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
