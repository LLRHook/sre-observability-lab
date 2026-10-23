"""HTTP-level tests for the copilot: the Alertmanager webhook must answer 2xx.

Regression for the first game day: the handler logged `extra={**result}` where
`result` has a key named "created", which collides with logging.LogRecord.created.
The pipeline had already done its work, the logger raised, the webhook returned 500
and Alertmanager retried ten times a minute.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from copilot.main import app
from tests.conftest import make_alert, make_webhook


def _client(lab) -> TestClient:
    app.state.pipeline = lab.pipeline
    return TestClient(app)


def test_webhook_returns_200_and_logs_without_raising(lab, caplog):
    client = _client(lab)
    with caplog.at_level(logging.INFO, logger="ops-copilot"):
        r = client.post("/alertmanager/webhook/pager", json=make_webhook(make_alert()).model_dump())
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["created"]) == 1 and body["deduped"] == [] and body["resolved"] == []
    handled = [rec for rec in caplog.records if rec.getMessage() == "webhook handled"]
    assert len(handled) == 1
    assert handled[0].incidents_created == body["created"]


def test_webhook_dedupes_and_resolves_over_http(lab):
    client = _client(lab)
    first = client.post(
        "/alertmanager/webhook/pager", json=make_webhook(make_alert()).model_dump()
    ).json()
    again = client.post("/alertmanager/webhook/pager", json=make_webhook(make_alert()).model_dump())
    assert again.status_code == 200
    assert again.json()["deduped"] == first["created"]
    resolved = client.post(
        "/alertmanager/webhook/pager",
        json=make_webhook(make_alert(status="resolved")).model_dump(),
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] == first["created"]
    assert client.get(f"/incidents/{first['created'][0]}").json()["status"] == "resolved"


def test_webhook_rejects_malformed_payload(lab):
    client = _client(lab)
    r = client.post("/alertmanager/webhook/pager", json={"alerts": "nope"})
    assert r.status_code == 422
