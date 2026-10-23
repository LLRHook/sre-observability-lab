#!/usr/bin/env python3
"""Alert noise report: the evidence base for pruning low-value alerts.

Reads the ALERTS time series from Prometheus over a window and reconstructs, per
alert (alertname + identifying labels):

  firings        how many distinct firing episodes
  flaps          episodes shorter than --flap-seconds (fired and cleared quickly)
  total_firing   total time in state=firing
  median/max ttr time-to-resolve per episode (resolve = next sample gap)
  pending_only   episodes that were pending but never fired (the `for` clause did its job)

Usage:
    python tools/alert_noise_report.py --window 24h [--prometheus http://localhost:9090] [--json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

IDENT_LABELS = ("alertname", "severity", "service", "slo", "job", "instance")


@dataclass
class Episode:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class AlertStats:
    key: str
    labels: dict[str, str]
    firing: list[Episode] = field(default_factory=list)
    pending: list[Episode] = field(default_factory=list)

    def summary(self, flap_seconds: float) -> dict[str, Any]:
        durations = [e.duration for e in self.firing]
        return {
            "alert": self.key,
            "severity": self.labels.get("severity", "-"),
            "firings": len(self.firing),
            "flaps": sum(1 for d in durations if d < flap_seconds),
            "pending_only": len(self.pending),
            "total_firing_s": round(sum(durations)),
            "median_ttr_s": round(statistics.median(durations)) if durations else 0,
            "max_ttr_s": round(max(durations)) if durations else 0,
        }


def parse_window(text: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1] not in units or not text[:-1].isdigit():
        raise ValueError(f"bad window {text!r}")
    return int(text[:-1]) * units[text[-1]]


def episodes_from_samples(values: list[list[Any]], step: float) -> list[Episode]:
    """Collapse a list of [ts, "1"] samples into contiguous episodes.

    A gap larger than 1.5 steps between consecutive samples ends an episode.
    """
    episodes: list[Episode] = []
    current: Episode | None = None
    for ts, _ in values:
        ts = float(ts)
        if current is None:
            current = Episode(ts, ts + step)
        elif ts - current.end <= step * 0.5:
            current.end = ts + step
        else:
            episodes.append(current)
            current = Episode(ts, ts + step)
    if current is not None:
        episodes.append(current)
    return episodes


def build_stats(matrix: list[dict[str, Any]], step: float) -> dict[str, AlertStats]:
    stats: dict[str, AlertStats] = {}
    for series in matrix:
        metric = series["metric"]
        state = metric.get("alertstate", "firing")
        ident = {k: metric[k] for k in IDENT_LABELS if k in metric}
        key = (
            metric.get("alertname", "?")
            + "{"
            + ",".join(f"{k}={v}" for k, v in ident.items() if k != "alertname")
            + "}"
        )
        entry = stats.setdefault(key, AlertStats(key, ident))
        episodes = episodes_from_samples(series["values"], step)
        if state == "firing":
            entry.firing.extend(episodes)
        else:
            entry.pending.extend(episodes)
    # pending_only = pending episodes that never overlap a firing one
    for entry in stats.values():
        entry.pending = [
            p
            for p in entry.pending
            if not any(f.start <= p.end and p.start <= f.end for f in entry.firing)
        ]
    return stats


def fetch_alerts(prometheus: str, window_s: int, step: int) -> list[dict[str, Any]]:
    end = time.time()
    start = end - window_s
    with httpx.Client(base_url=prometheus, timeout=30) as client:
        resp = client.get(
            "/api/v1/query_range",
            params={"query": "ALERTS", "start": start, "end": end, "step": step},
        )
        resp.raise_for_status()
        payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(payload)
    return payload["data"]["result"]


def render_table(rows: list[dict[str, Any]]) -> str:
    cols = [
        "alert",
        "severity",
        "firings",
        "flaps",
        "pending_only",
        "total_firing_s",
        "median_ttr_s",
        "max_ttr_s",
    ]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) if rows else len(c) for c in cols}
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    out = [line, "-" * len(line)]
    for r in rows:
        out.append("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    return "\n".join(out)


def recommendations(rows: list[dict[str, Any]], flap_seconds: float) -> list[str]:
    notes = []
    for r in rows:
        if r["firings"] and r["flaps"] / r["firings"] >= 0.5:
            notes.append(
                f"{r['alert']}: {r['flaps']}/{r['firings']} episodes shorter than {flap_seconds:.0f}s "
                "- flapping; lengthen `for`, widen the short window, or lower severity to ticket."
            )
        if r["firings"] >= 5 and r["median_ttr_s"] < flap_seconds:
            notes.append(
                f"{r['alert']}: fires often and self-resolves fast - candidate for a ticket, not a page."
            )
        if r["pending_only"] >= 3 and r["firings"] == 0:
            notes.append(
                f"{r['alert']}: pending {r['pending_only']}x but never fired - the `for` clause is absorbing noise."
            )
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Alert noise report from Prometheus ALERTS history"
    )
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--window", default="24h")
    parser.add_argument(
        "--step", type=int, default=15, help="query step in seconds (match scrape interval)"
    )
    parser.add_argument("--flap-seconds", type=float, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    matrix = fetch_alerts(args.prometheus, parse_window(args.window), args.step)
    stats = build_stats(matrix, args.step)
    rows = sorted(
        (s.summary(args.flap_seconds) for s in stats.values()),
        key=lambda r: (-r["firings"], -r["pending_only"]),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "window": args.window,
                    "alerts": rows,
                    "recommendations": recommendations(rows, args.flap_seconds),
                },
                indent=2,
            )
        )
        return 0
    print(f"Alert noise report: last {args.window} from {args.prometheus} (step {args.step}s)\n")
    print(render_table(rows) if rows else "no alert history in window")
    notes = recommendations(rows, args.flap_seconds)
    if notes:
        print("\nRecommendations:")
        for n in notes:
            print(f"  - {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
