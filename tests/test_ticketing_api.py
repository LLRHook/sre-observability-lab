"""ticketing-api behaviour with the database faked out."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ADMIN_TOKEN", "test-token")

from app import db, main  # noqa: E402
from app.chaos import chaos  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(db, "open_pool", lambda: None)
    monkeypatch.setattr(db, "close_pool", lambda: None)
    monkeypatch.setattr(db, "ensure_schema", lambda: None)
    monkeypatch.setattr(db, "list_events", lambda: [{"id": 1, "name": "Opening Night"}])
    monkeypatch.setattr(
        db,
        "create_order",
        lambda e, q, c: {"id": 7, "event_id": e, "quantity": q, "customer": c, "created_at": "x"},
    )
    monkeypatch.setattr(db, "get_order", lambda i: {"id": i} if i < 100 else None)
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-token")
    chaos.reset()
    with TestClient(main.app) as c:
        yield c
    chaos.reset()


ADMIN = {"x-admin-token": "test-token"}


def test_requests_get_a_request_id_and_metrics(client):
    r = client.get("/events")
    assert r.status_code == 200 and r.headers["x-request-id"]
    r = client.get("/events", headers={"x-request-id": "trace-123"})
    assert r.headers["x-request-id"] == "trace-123"
    metrics = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/events",status="200"}' in metrics
    assert 'http_request_duration_seconds_bucket{le="0.5",route="/events"}' in metrics
    assert "http_requests_in_flight 0.0" in metrics


def test_route_template_not_concrete_path_in_labels(client):
    client.get("/orders/42")
    client.get("/orders/43")
    metrics = client.get("/metrics").text
    assert 'route="/orders/{order_id}"' in metrics
    assert 'route="/orders/42"' not in metrics


def test_purchase_and_404(client):
    r = client.post("/purchase", json={"event_id": 1, "quantity": 2, "customer": "ada"})
    assert r.status_code == 201 and r.json()["id"] == 7
    assert client.get("/orders/999").status_code == 404
    assert (
        client.post(
            "/purchase", json={"event_id": 1, "quantity": 99, "customer": "ada"}
        ).status_code
        == 422
    )


def test_chaos_requires_token(client):
    assert client.get("/admin/chaos").status_code == 401
    assert client.post("/admin/chaos", json={"error_rate": 1}).status_code == 401
    assert (
        client.post(
            "/admin/chaos", json={"error_rate": 1}, headers={"x-admin-token": "nope"}
        ).status_code
        == 401
    )


def test_chaos_error_injection_and_reset(client):
    r = client.post("/admin/chaos", json={"error_rate": 1.0}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["error_rate"] == 1.0
    assert client.get("/events").status_code == 500
    assert client.get("/healthz").status_code == 200  # health is exempt
    assert client.get("/admin/chaos", headers=ADMIN).status_code == 200  # admin is exempt
    metrics = client.get("/metrics").text
    assert "chaos_error_rate 1.0" in metrics
    r = client.post("/admin/chaos/reset", headers=ADMIN)
    assert r.json() == {"error_rate": 0.0, "latency_ms": 0, "db_fault": False}
    assert client.get("/events").status_code == 200


def test_chaos_validation_bounds(client):
    assert client.post("/admin/chaos", json={"error_rate": 1.5}, headers=ADMIN).status_code == 422
    assert client.post("/admin/chaos", json={"latency_ms": -1}, headers=ADMIN).status_code == 422


def test_db_fault_flag_propagates(client, monkeypatch):
    seen = []
    monkeypatch.setattr(db, "set_fault", lambda enabled: seen.append(enabled))
    client.post("/admin/chaos", json={"db_fault": True}, headers=ADMIN)
    client.post("/admin/chaos/reset", headers=ADMIN)
    assert seen == [True, False]


def test_json_log_line_shape(client):
    """Format at emit time (as uvicorn does) so the request-id contextvar is still set."""
    import io
    import logging

    from app.logging_setup import JsonFormatter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("ticketing-api")
    logger.addHandler(handler)
    try:
        client.get("/events", headers={"x-request-id": "rid-1"})
    finally:
        logger.removeHandler(handler)
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    line = next(entry for entry in lines if entry.get("event") == "request")
    assert (
        line["service"] == "ticketing-api" and line["route"] == "/events" and line["status"] == 200
    )
    assert "duration_ms" in line and line["ts"].endswith("Z") and line["request_id"] == "rid-1"
