#!/usr/bin/env python3
"""Tiny CLI for the human side of the loop.

copilotctl list
copilotctl show <incident-id>
copilotctl approve <incident-id> --approver "Name" [--action 0] [--dry-run]
copilotctl reject  <incident-id> --approver "Name" --reason "..."
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copilot", default=os.environ.get("COPILOT_URL", "http://localhost:8001"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("incident_id")
    approve = sub.add_parser("approve")
    approve.add_argument("incident_id")
    approve.add_argument("--approver", required=True)
    approve.add_argument("--action", type=int, default=0)
    approve.add_argument("--dry-run", action="store_true")
    reject = sub.add_parser("reject")
    reject.add_argument("incident_id")
    reject.add_argument("--approver", required=True)
    reject.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    with httpx.Client(base_url=args.copilot, timeout=15) as client:
        if args.cmd == "list":
            r = client.get("/incidents")
            r.raise_for_status()
            for inc in r.json():
                note = inc.get("triage") or {}
                actions = [a["action"] for a in note.get("proposed_actions", [])]
                print(
                    f"{inc['id']}  {inc['status']:<9} {inc['alertname']:<42} {note.get('classification', '-'):<20} actions={actions}"
                )
            return 0
        if args.cmd == "show":
            r = client.get(f"/incidents/{args.incident_id}")
        elif args.cmd == "approve":
            r = client.post(
                f"/incidents/{args.incident_id}/approve",
                json={
                    "approver": args.approver,
                    "action_index": args.action,
                    "dry_run": args.dry_run,
                },
            )
        else:
            r = client.post(
                f"/incidents/{args.incident_id}/reject",
                json={"approver": args.approver, "reason": args.reason},
            )
        if r.status_code >= 400:
            print(f"error {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        print(json.dumps(r.json(), indent=2, default=str))
        return 0


if __name__ == "__main__":
    sys.exit(main())
