"""Steady synthetic traffic against ticketing-api.

Roughly RATE requests/second, mixed across the public endpoints, with a
Prometheus endpoint of its own so the loadgen's view of the world can be
compared to the server's view. Dependency-free beyond httpx + prometheus_client.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time

import httpx
from prometheus_client import Counter, Histogram, start_http_server

TARGET = os.environ.get("TARGET_URL", "http://ticketing-api:8000")
RATE = float(os.environ.get("RATE_PER_SEC", "10"))
TIMEOUT = float(os.environ.get("TIMEOUT_SECONDS", "5"))

CLIENT_REQUESTS = Counter("loadgen_requests_total", "Client-side outcomes", ["route", "outcome"])
CLIENT_LATENCY = Histogram("loadgen_request_duration_seconds", "Client-side latency", ["route"])

NAMES = ["ada", "grace", "linus", "ken", "barbara", "dennis", "margaret", "radia"]


def log(**fields: object) -> None:
    fields.setdefault("service", "loadgen")
    fields.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    print(json.dumps(fields), flush=True)


def one_request(client: httpx.Client) -> None:
    roll = random.random()
    if roll < 0.55:
        route, call = (
            "/purchase",
            lambda: client.post(
                "/purchase",
                json={
                    "event_id": random.randint(1, 3),
                    "quantity": random.randint(1, 4),
                    "customer": random.choice(NAMES),
                },
            ),
        )
    elif roll < 0.85:
        route, call = "/events", lambda: client.get("/events")
    else:
        route, call = "/orders/{order_id}", lambda: client.get(f"/orders/{random.randint(1, 50)}")
    start = time.perf_counter()
    try:
        resp = call()
        outcome = "ok" if resp.status_code < 500 else "server_error"
    except httpx.HTTPError as exc:
        outcome = "transport_error"
        log(level="WARNING", msg="request failed", route=route, error=str(exc))
    CLIENT_LATENCY.labels(route).observe(time.perf_counter() - start)
    CLIENT_REQUESTS.labels(route, outcome).inc()


WORKERS = 2


def worker(stop: threading.Event) -> None:
    # Each worker contributes RATE / WORKERS so the total is RATE.
    interval = WORKERS / RATE if RATE > 0 else 1.0
    with httpx.Client(base_url=TARGET, timeout=TIMEOUT) as client:
        while not stop.is_set():
            one_request(client)
            # Jitter so the scrape never sees a perfectly periodic signal.
            time.sleep(max(0.0, random.gauss(interval, interval / 4)))


def main() -> int:
    start_http_server(int(os.environ.get("METRICS_PORT", "8002")))
    log(level="INFO", msg="loadgen starting", target=TARGET, rate_per_sec=RATE)
    stop = threading.Event()
    threads = [threading.Thread(target=worker, args=(stop,), daemon=True) for _ in range(WORKERS)]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
