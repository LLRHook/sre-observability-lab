"""Toy ticket-purchase service.

Three things matter here for the lab:

1. Prometheus instrumentation (requests by route/status, a latency histogram, an
   in-flight gauge) exposed on /metrics.
2. Structured JSON logs with a request id on every line, so the logging pipeline
   can correlate a log line with a metric spike.
3. Token-protected chaos endpoints so a game day can inject an error rate,
   latency or a database fault on purpose, and reset it.
"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

from app import db
from app.chaos import ChaosState, chaos
from app.logging_setup import configure_logging, request_id_var

log = logging.getLogger("ticketing-api")

# --- Prometheus metrics ----------------------------------------------------
# Labels are deliberately low-cardinality: route is the *template* (/orders/{id}),
# never the concrete path, so a million order ids do not become a million series.
REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests by route template, method and status code",
    ["route", "method", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request latency by route template",
    ["route"],
    # Buckets straddle the 500 ms latency SLO threshold used in slo/slos.yaml.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
IN_FLIGHT = Gauge("http_requests_in_flight", "Requests currently being handled")
PURCHASES = Counter("ticket_purchases_total", "Ticket purchases by outcome", ["outcome"])
CHAOS_ERROR_RATE = Gauge("chaos_error_rate", "Injected error fraction (0..1)")
CHAOS_LATENCY_MS = Gauge("chaos_latency_ms", "Injected latency in milliseconds")
CHAOS_DB_FAULT = Gauge("chaos_db_fault", "1 when database faults are injected")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    db.open_pool()
    db.ensure_schema()
    log.info("startup complete", extra={"event": "startup"})
    yield
    db.close_pool()


app = FastAPI(title="ticketing-api", version="0.1.0", lifespan=lifespan)


# --- Middleware: request id, metrics, access log ---------------------------
@app.middleware("http")
async def observe(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    IN_FLIGHT.inc()
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["x-request-id"] = rid
        return response
    except Exception:
        log.exception("unhandled error", extra={"event": "unhandled_error"})
        return JSONResponse({"error": "internal", "request_id": rid}, status_code=500)
    finally:
        elapsed = time.perf_counter() - start
        route = _route_template(request)
        REQUESTS.labels(route, request.method, str(status)).inc()
        LATENCY.labels(route).observe(elapsed)
        IN_FLIGHT.dec()
        log.info(
            "request",
            extra={
                "event": "request",
                "method": request.method,
                "route": route,
                "path": request.url.path,
                "status": status,
                "duration_ms": round(elapsed * 1000, 2),
            },
        )
        request_id_var.reset(token)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


# --- Chaos hooks -------------------------------------------------------------
async def apply_chaos() -> None:
    """Called by the customer-facing endpoints only; admin and health are exempt."""
    state = chaos.get()
    if state.latency_ms > 0:
        await anyio.sleep(state.latency_ms / 1000)
    if state.error_rate > 0 and random.random() < state.error_rate:
        log.error(
            "injected failure",
            extra={"event": "chaos_error", "error_rate": state.error_rate},
        )
        raise HTTPException(status_code=500, detail="injected failure")


# --- Public API ------------------------------------------------------------
class PurchaseRequest(BaseModel):
    event_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=8)
    customer: str = Field(min_length=1, max_length=64)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    try:
        db.ping()
    except Exception as exc:  # pragma: no cover - exercised in the lab, not unit tests
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready"}


@app.get("/events")
async def list_events() -> list[dict]:
    await apply_chaos()
    return await anyio.to_thread.run_sync(db.list_events)


@app.post("/purchase", status_code=201)
async def purchase(body: PurchaseRequest) -> dict:
    await apply_chaos()
    try:
        order = await anyio.to_thread.run_sync(
            db.create_order, body.event_id, body.quantity, body.customer
        )
    except db.SoldOut as exc:
        PURCHASES.labels("sold_out").inc()
        raise HTTPException(status_code=409, detail="sold out") from exc
    except db.DatabaseFault:
        PURCHASES.labels("db_error").inc()
        log.error("database error during purchase", extra={"event": "db_error"})
        raise HTTPException(status_code=500, detail="database error") from None
    PURCHASES.labels("ok").inc()
    log.info(
        "purchase ok",
        extra={"event": "purchase", "order_id": order["id"], "event_id": body.event_id},
    )
    return order


@app.get("/orders/{order_id}")
async def get_order(order_id: int) -> dict:
    await apply_chaos()
    try:
        order = await anyio.to_thread.run_sync(db.get_order, order_id)
    except db.DatabaseFault:
        raise HTTPException(status_code=500, detail="database error") from None
    if order is None:
        raise HTTPException(status_code=404, detail="not found")
    return order


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Admin / chaos (token protected) ---------------------------------------
def require_admin(x_admin_token: str = Header(default="")) -> None:
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="admin token required")


class ChaosRequest(BaseModel):
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms: int | None = Field(default=None, ge=0, le=10_000)
    db_fault: bool | None = None


def _publish_chaos(state: ChaosState) -> None:
    CHAOS_ERROR_RATE.set(state.error_rate)
    CHAOS_LATENCY_MS.set(state.latency_ms)
    CHAOS_DB_FAULT.set(1 if state.db_fault else 0)
    db.set_fault(state.db_fault)


@app.get("/admin/chaos", dependencies=[Depends(require_admin)])
def get_chaos() -> dict:
    return chaos.get().model_dump()


@app.post("/admin/chaos", dependencies=[Depends(require_admin)])
def set_chaos(body: ChaosRequest) -> dict:
    state = chaos.update(**body.model_dump(exclude_none=True))
    _publish_chaos(state)
    log.warning("chaos updated", extra={"event": "chaos_update", **state.model_dump()})
    return state.model_dump()


@app.post("/admin/chaos/reset", dependencies=[Depends(require_admin)])
def reset_chaos() -> dict:
    state = chaos.reset()
    _publish_chaos(state)
    log.warning("chaos reset", extra={"event": "chaos_reset"})
    return state.model_dump()
