"""End-to-end copilot flow with fakes: webhook -> triage -> approval -> execution -> resolve."""

from __future__ import annotations

import json

from copilot.models import IncidentStatus
from copilot.pipeline import ApprovalError
from tests.conftest import make_alert, make_webhook


def test_firing_alert_creates_incident_with_triage_note(lab):
    result = lab.pipeline.handle_webhook(make_webhook(make_alert()))
    assert len(result["created"]) == 1 and not result["deduped"]
    inc = lab.store.get(result["created"][0])
    assert inc.status is IncidentStatus.TRIAGED
    assert inc.triage is not None
    assert inc.triage.classification.value == "injected_fault"
    assert inc.triage.summarizer == "rules"
    actions = [a.action for a in inc.triage.proposed_actions]
    assert actions[0] == "reset_chaos" and "acknowledge" in actions
    # Context gathering was deterministic and read-only: only GETs against the fakes.
    assert any("slo:burn_rate:ratio_rate1h" in q for q in lab.prom.queries)
    assert lab.logs.bodies and lab.logs.bodies[0]["query"]["bool"]["filter"][2] == {
        "term": {"service": "ticketing-api"}
    }
    assert inc.context["runbook"]["found"] is True


def test_triage_note_is_posted_to_incident_channel_and_audited(lab):
    result = lab.pipeline.handle_webhook(make_webhook(make_alert()))
    inc_id = result["created"][0]
    channel = lab.channel_path.read_text()
    assert f"TRIAGE {inc_id}" in channel and "reset_chaos" in channel
    steps = [json.loads(line)["step"] for line in lab.audit_path.read_text().splitlines()]
    assert steps[:3] == ["alert_received", "context_gathered", "triage_note"]
    assert all(json.loads(line)["ts"] for line in lab.audit_path.read_text().splitlines())


def test_duplicate_fingerprint_is_deduped(lab):
    first = lab.pipeline.handle_webhook(make_webhook(make_alert()))
    second = lab.pipeline.handle_webhook(make_webhook(make_alert()))
    assert second["created"] == [] and second["deduped"] == first["created"]
    assert len(lab.store.list()) == 1


def test_nothing_executes_without_approval(lab):
    lab.pipeline.handle_webhook(make_webhook(make_alert()))
    assert lab.executor.calls == []


def test_approval_requires_identity_and_executes_once(lab):
    inc_id = lab.pipeline.handle_webhook(make_webhook(make_alert()))["created"][0]
    try:
        lab.pipeline.approve(inc_id, approver="   ", action_index=0, dry_run=False)
        raise AssertionError("blank approver accepted")
    except ApprovalError:
        pass
    assert lab.executor.calls == []

    record = lab.pipeline.approve(inc_id, approver="Victor Ivanov", action_index=0, dry_run=False)
    assert record["action"] == "reset_chaos" and record["approver"] == "Victor Ivanov"
    assert [(a.action, d) for a, d in lab.executor.calls] == [("reset_chaos", False)]
    inc = lab.store.get(inc_id)
    assert inc.status is IncidentStatus.EXECUTED
    audit = [json.loads(line) for line in lab.audit_path.read_text().splitlines()]
    granted = [e for e in audit if e["step"] == "approval_granted"]
    assert granted and granted[0]["actor"] == "Victor Ivanov"
    assert any(e["step"] == "executed" for e in audit)


def test_dry_run_does_not_change_state(lab):
    inc_id = lab.pipeline.handle_webhook(make_webhook(make_alert()))["created"][0]
    record = lab.pipeline.approve(inc_id, approver="victor", action_index=0, dry_run=True)
    assert record["dry_run"] is True and lab.executor.calls[0][1] is True
    assert lab.store.get(inc_id).status is IncidentStatus.TRIAGED


def test_out_of_range_action_index_is_refused(lab):
    inc_id = lab.pipeline.handle_webhook(make_webhook(make_alert()))["created"][0]
    try:
        lab.pipeline.approve(inc_id, approver="victor", action_index=99, dry_run=False)
        raise AssertionError("out of range index accepted")
    except ApprovalError:
        pass
    assert lab.executor.calls == []


def test_reject_records_decision(lab):
    inc_id = lab.pipeline.handle_webhook(make_webhook(make_alert()))["created"][0]
    lab.pipeline.reject(inc_id, approver="victor", reason="not convinced")
    inc = lab.store.get(inc_id)
    assert inc.status is IncidentStatus.REJECTED and inc.approvals[-1]["rejected"] is True
    assert lab.executor.calls == []


def test_resolved_webhook_closes_incident(lab):
    inc_id = lab.pipeline.handle_webhook(make_webhook(make_alert()))["created"][0]
    result = lab.pipeline.handle_webhook(make_webhook(make_alert(status="resolved")))
    assert result["resolved"] == [inc_id]
    inc = lab.store.get(inc_id)
    assert inc.status is IncidentStatus.RESOLVED and inc.resolved_at is not None
    # A new firing with the same fingerprint after resolution is a new incident.
    again = lab.pipeline.handle_webhook(make_webhook(make_alert()))
    assert again["created"] and again["created"][0] != inc_id


def test_context_gathering_survives_opensearch_outage(lab):
    lab.logs.fail = True
    inc_id = lab.pipeline.handle_webhook(make_webhook(make_alert()))["created"][0]
    inc = lab.store.get(inc_id)
    assert inc.triage is not None
    assert inc.context["logs"]["error"] == "ConnectionError"


def test_unsafe_label_values_never_reach_promql(lab):
    evil = make_alert(fingerprint="evil", service='x"} or vector(1) #')
    lab.pipeline.handle_webhook(make_webhook(evil))
    assert not any("vector(1)" in q for q in lab.prom.queries)
