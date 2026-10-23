#!/usr/bin/env python3
"""Game day driver: baseline -> inject -> page -> triage -> approval -> reset -> resolve.

Every phase is observed from the real APIs (Prometheus, Alertmanager, ops-copilot,
ticketing-api), and the timeline is built from their timestamps, not from sleeps.

Usage:
    python tools/gameday.py --approver "Victor Ivanov"        # waits for `make approve`
    python tools/gameday.py --approver "Victor Ivanov" --auto-approve
    python tools/gameday.py --error-rate 0.5 --out docs/gameday

Environment: ADMIN_TOKEN (ticketing-api admin token), or pass --admin-token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

PAGE_ALERT = "TicketingApiAvailabilityBurnRatePage"


class Timeline:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.t0 = time.time()

    def mark(self, phase: str, detail: str, at: float | None = None, **extra: Any) -> None:
        ts = at if at is not None else time.time()
        entry = {
            "t_plus_s": round(ts - self.t0, 1),
            "ts": datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds"),
            "phase": phase,
            "detail": detail,
            **extra,
        }
        self.events.append(entry)
        print(f"[{entry['t_plus_s']:>7.1f}s] {phase:<22} {detail}", flush=True)


def wait_until(desc: str, fn, timeout: float, interval: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {desc}")


class Lab:
    def __init__(self, args: argparse.Namespace) -> None:
        self.prom = httpx.Client(base_url=args.prometheus, timeout=10)
        self.am = httpx.Client(base_url=args.alertmanager, timeout=10)
        self.copilot = httpx.Client(base_url=args.copilot, timeout=10)
        self.api = httpx.Client(base_url=args.ticketing, timeout=10)
        self.token = args.admin_token

    def query(self, promql: str) -> list[dict[str, Any]]:
        r = self.prom.get("/api/v1/query", params={"query": promql})
        r.raise_for_status()
        return r.json()["data"]["result"]

    def scalar(self, promql: str) -> float | None:
        res = self.query(promql)
        return float(res[0]["value"][1]) if res else None

    def targets_down(self) -> list[str]:
        r = self.prom.get("/api/v1/targets")
        r.raise_for_status()
        return [
            f"{t['labels'].get('job')}/{t['labels'].get('instance')}"
            for t in r.json()["data"]["activeTargets"]
            if t["health"] != "up"
        ]

    def prom_alert(self, name: str, state: str) -> dict[str, Any] | None:
        r = self.prom.get("/api/v1/alerts")
        r.raise_for_status()
        for a in r.json()["data"]["alerts"]:
            if a["labels"].get("alertname") == name and a["state"] == state:
                return a
        return None

    def am_alert(self, name: str) -> dict[str, Any] | None:
        r = self.am.get(
            "/api/v2/alerts", params={"active": "true", "silenced": "false", "inhibited": "false"}
        )
        r.raise_for_status()
        for a in r.json():
            if a["labels"].get("alertname") == name:
                return a
        return None

    def am_alert_any(self, name: str) -> dict[str, Any] | None:
        r = self.am.get("/api/v2/alerts")
        r.raise_for_status()
        for a in r.json():
            if a["labels"].get("alertname") == name:
                return a
        return None

    def set_chaos(self, **body: Any) -> dict[str, Any]:
        r = self.api.post("/admin/chaos", json=body, headers={"x-admin-token": self.token})
        r.raise_for_status()
        return r.json()

    def chaos(self) -> dict[str, Any]:
        r = self.api.get("/admin/chaos", headers={"x-admin-token": self.token})
        r.raise_for_status()
        return r.json()

    def incident_for(self, name: str) -> dict[str, Any] | None:
        r = self.copilot.get("/incidents")
        r.raise_for_status()
        for inc in r.json():
            if inc["alertname"] == name and inc["status"] != "resolved":
                return inc
        return None

    def incident(self, incident_id: str) -> dict[str, Any]:
        r = self.copilot.get(f"/incidents/{incident_id}")
        r.raise_for_status()
        return r.json()

    def approve(self, incident_id: str, approver: str, action_index: int) -> dict[str, Any]:
        r = self.copilot.post(
            f"/incidents/{incident_id}/approve",
            json={"approver": approver, "action_index": action_index, "dry_run": False},
        )
        r.raise_for_status()
        return r.json()


def run(args: argparse.Namespace) -> int:
    lab = Lab(args)
    tl = Timeline()
    tl.mark("baseline", "checking Prometheus targets and SLI")
    down = lab.targets_down()
    if down:
        tl.mark("baseline", f"targets not up: {down}")
        return 2
    sli = lab.scalar('slo:sli_error:ratio_rate5m{slo="ticketing-api-availability"}')
    budget = lab.scalar('slo:error_budget_remaining:ratio{slo="ticketing-api-availability"}')
    tl.mark(
        "baseline",
        f"all targets up; 5m error ratio={sli if sli is None else round(sli, 5)}; "
        f"30d budget remaining={budget if budget is None else round(budget, 3)}",
    )
    if lab.prom_alert(PAGE_ALERT, "firing") or lab.incident_for(PAGE_ALERT):
        tl.mark(
            "baseline", f"{PAGE_ALERT} already firing or an incident is open; reset and wait first"
        )
        return 2
    chaos = lab.chaos()
    if chaos["error_rate"] or chaos["latency_ms"] or chaos["db_fault"]:
        tl.mark("baseline", f"chaos already active: {chaos}; resetting")
        lab.api.post("/admin/chaos/reset", headers={"x-admin-token": lab.token}).raise_for_status()

    state = lab.set_chaos(error_rate=args.error_rate)
    tl.mark("inject", f"chaos set: {state}")

    pending = wait_until("alert pending", lambda: lab.prom_alert(PAGE_ALERT, "pending"), 600)
    tl.mark(
        "detect",
        f"{PAGE_ALERT} pending in Prometheus (for: 2m starts)",
        activeAt=pending["activeAt"],
    )

    firing = wait_until("alert firing", lambda: lab.prom_alert(PAGE_ALERT, "firing"), 600)
    tl.mark(
        "page",
        f"{PAGE_ALERT} FIRING in Prometheus",
        activeAt=firing["activeAt"],
        value=firing.get("value"),
    )

    am = wait_until("alertmanager", lambda: lab.am_alert(PAGE_ALERT), 300)
    tl.mark(
        "route",
        f"Alertmanager has it: receivers={[r['name'] for r in am['receivers']]} state={am['status']['state']}",
        startsAt=am["startsAt"],
    )

    inc = wait_until("copilot incident", lambda: lab.incident_for(PAGE_ALERT), 300)
    full = lab.incident(inc["id"])
    note = full["triage"]
    tl.mark(
        "triage",
        f"incident {inc['id']} [{note['summarizer']}] classification={note['classification']} "
        f"confidence={note['confidence']} actions={[a['action'] for a in note['proposed_actions']]}",
        created_at=full["created_at"],
    )
    for ev in note["evidence"]:
        tl.mark("triage.evidence", f"[{ev['source']}] {ev['detail']}")

    reset_idx = next(
        (i for i, a in enumerate(note["proposed_actions"]) if a["action"] == "reset_chaos"), None
    )
    if reset_idx is None:
        tl.mark(
            "approval", "copilot did not propose reset_chaos; stopping here (manual reset needed)"
        )
        return 3

    if args.auto_approve:
        tl.mark(
            "approval", f"approving action [{reset_idx}] reset_chaos as {args.approver!r} (auto)"
        )
        rec = lab.approve(inc["id"], args.approver, reset_idx)
    else:
        print(
            f'\n>>> Waiting for a human. In another shell run:\n>>>   make approve INCIDENT={inc["id"]} ACTION={reset_idx} APPROVER="{args.approver}"\n',
            flush=True,
        )
        rec_inc = wait_until(
            "human approval",
            lambda: (
                lab.incident(inc["id"])
                if lab.incident(inc["id"])["status"] in ("executed", "rejected")
                else None
            ),
            args.approval_timeout,
            interval=3,
        )
        rec = rec_inc["approvals"][-1]
        if rec.get("rejected"):
            tl.mark("approval", f"rejected by {rec['approver']}: {rec.get('reason')}")
            return 3
    tl.mark(
        "approval",
        f"approved by {rec['approver']}; executed {rec['action']} -> {rec['outcome']['result']}",
        approved_at=rec["ts"],
    )

    chaos = lab.chaos()
    tl.mark("mitigate", f"chaos state now: {chaos}")
    assert chaos["error_rate"] == 0

    def recovered():
        v = lab.scalar('slo:burn_rate:ratio_rate5m{slo="ticketing-api-availability"}')
        return v is not None and v < 14.4

    wait_until("5m burn rate below 14.4", recovered, 900, interval=10)
    tl.mark("recover", "5m burn rate below page threshold")

    # The 1h/5m pair releases within ~5 minutes of the fix, but the 6h/30m pair keeps
    # the page up until the burn leaves the 30m window: expect roughly half an hour.
    print(">>> Waiting for the page to clear; the 30m short window makes this ~30 min.", flush=True)
    wait_until(
        "prometheus alert cleared",
        lambda: lab.prom_alert(PAGE_ALERT, "firing") is None,
        2700,
        interval=10,
    )
    tl.mark("resolve", f"{PAGE_ALERT} no longer firing in Prometheus")

    wait_until(
        "copilot resolved",
        lambda: lab.incident(inc["id"])["status"] == "resolved",
        600,
        interval=10,
    )
    final = lab.incident(inc["id"])
    tl.mark(
        "resolve",
        f"incident {inc['id']} resolved via Alertmanager resolved webhook",
        resolved_at=final["resolved_at"],
    )

    ticket = lab.am_alert_any("TicketingApiAvailabilityBurnRateTicket")
    if ticket:
        tl.mark(
            "note",
            f"slow-burn ticket alert state={ticket['status']['state']} inhibitedBy={ticket['status'].get('inhibitedBy')}",
        )

    write_report(args, tl, final)
    return 0


def write_report(args: argparse.Namespace, tl: Timeline, incident: dict[str, Any]) -> None:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(tl.t0, UTC).strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}-timeline.json").write_text(
        json.dumps({"events": tl.events, "incident": incident}, indent=2, default=str)
    )
    lines = [
        f"# Game day {stamp}",
        "",
        f"Injected error rate: {args.error_rate}. Approver: {args.approver}. Auto-approve: {args.auto_approve}.",
        "",
        "| T+ (s) | UTC | Phase | Detail |",
        "|---:|---|---|---|",
    ]
    for e in tl.events:
        lines.append(
            f"| {e['t_plus_s']} | {e['ts']} | {e['phase']} | {e['detail'].replace('|', '/')} |"
        )
    lines += [
        "",
        "## Triage note",
        "",
        "```json",
        json.dumps(incident.get("triage"), indent=2),
        "```",
        "",
        "## Audit trail",
        "",
        "```json",
        json.dumps(incident.get("audit"), indent=2, default=str),
        "```",
    ]
    (out_dir / f"{stamp}.md").write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out_dir / (stamp + '.md')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--alertmanager", default="http://localhost:9093")
    parser.add_argument("--copilot", default="http://localhost:8001")
    parser.add_argument("--ticketing", default="http://localhost:8000")
    parser.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", ""))
    parser.add_argument("--error-rate", type=float, default=0.5)
    parser.add_argument(
        "--approver", default=os.environ.get("APPROVER") or os.environ.get("USER", "on-call")
    )
    parser.add_argument(
        "--auto-approve", action="store_true", help="approve from this script instead of waiting"
    )
    parser.add_argument("--approval-timeout", type=float, default=1800)
    parser.add_argument("--out", default="docs/gameday")
    args = parser.parse_args(argv)
    if not args.admin_token:
        print("ADMIN_TOKEN is required (set in .env)", file=sys.stderr)
        return 2
    try:
        return run(args)
    except TimeoutError as exc:
        print(f"\nGAME DAY FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
