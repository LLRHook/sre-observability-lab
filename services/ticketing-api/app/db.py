"""Postgres access through a small connection pool.

`set_fault(True)` makes every query raise DatabaseFault so the lab can simulate a
database outage without touching the real database.
"""

from __future__ import annotations

import os
import threading

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class DatabaseFault(RuntimeError):
    """Injected or real database failure."""


class SoldOut(RuntimeError):
    """Not enough inventory for the requested quantity."""


_pool: ConnectionPool | None = None
_fault = threading.Event()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    venue TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    sold INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    quantity INTEGER NOT NULL,
    customer TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO events (name, venue, capacity)
SELECT * FROM (VALUES
    ('Opening Night', 'Riverside Arena', 50000000),
    ('Matinee', 'Harbor Theatre', 50000000),
    ('Season Finale', 'Summit Stadium', 50000000)
) AS v(name, venue, capacity)
WHERE NOT EXISTS (SELECT 1 FROM events);
"""


def dsn() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://tickets:tickets@localhost:5432/tickets")


def open_pool() -> None:
    global _pool
    _pool = ConnectionPool(
        dsn(), min_size=1, max_size=8, open=True, kwargs={"row_factory": dict_row}
    )


def close_pool() -> None:
    if _pool is not None:
        _pool.close()


def set_fault(enabled: bool) -> None:
    if enabled:
        _fault.set()
    else:
        _fault.clear()


def _check_fault() -> None:
    if _fault.is_set():
        raise DatabaseFault("injected database fault")


def ensure_schema() -> None:
    assert _pool is not None
    with _pool.connection() as conn:
        conn.execute(SCHEMA)


def ping() -> None:
    assert _pool is not None
    with _pool.connection() as conn:
        conn.execute("SELECT 1")


def list_events() -> list[dict]:
    _check_fault()
    assert _pool is not None
    with _pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, name, venue, capacity, sold FROM events ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def create_order(event_id: int, quantity: int, customer: str) -> dict:
    _check_fault()
    assert _pool is not None
    try:
        with _pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT capacity, sold FROM events WHERE id = %s FOR UPDATE", (event_id,)
            ).fetchone()
            if row is None:
                raise SoldOut("no such event")
            if row["sold"] + quantity > row["capacity"]:
                raise SoldOut("sold out")
            conn.execute("UPDATE events SET sold = sold + %s WHERE id = %s", (quantity, event_id))
            order = conn.execute(
                "INSERT INTO orders (event_id, quantity, customer) VALUES (%s, %s, %s) "
                "RETURNING id, event_id, quantity, customer, created_at",
                (event_id, quantity, customer),
            ).fetchone()
    except SoldOut:
        raise
    except Exception as exc:
        raise DatabaseFault(str(exc)) from exc
    assert order is not None
    return dict(order)


def get_order(order_id: int) -> dict | None:
    _check_fault()
    assert _pool is not None
    try:
        with _pool.connection() as conn:
            row = conn.execute(
                "SELECT id, event_id, quantity, customer, created_at FROM orders WHERE id = %s",
                (order_id,),
            ).fetchone()
    except Exception as exc:
        raise DatabaseFault(str(exc)) from exc
    return dict(row) if row else None
