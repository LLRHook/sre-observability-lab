# Runbook: PostgresDown

Alert: `PostgresDown` (severity page, service postgres).

## What this alert means

`postgres_exporter` cannot connect to Postgres (`pg_up == 0`) for over a minute. This is a
*cause* alert: while it fires, Alertmanager inhibits every `service="ticketing-api"` alert
(see `inhibit_rules` in `alertmanager/alertmanager.yml`) so the on-call sees one page, not four.

## First five minutes

1. Is the container alive? `docker compose ps postgres`, `docker compose logs --tail=50 postgres`.
2. Is it only the exporter that cannot connect? `docker compose exec postgres pg_isready -U tickets`.
3. Is the app also failing? `sum(rate(ticket_purchases_total{outcome="db_error"}[1m]))` and the
   Postgres dashboard (`http://localhost:3000/d/postgres/postgres`).
4. Lab: is this a chaos DB fault instead? `max(chaos_db_fault)`; a chaos DB fault does **not**
   set `pg_up` to 0 (the database is fine, the app pretends it is not).

## Mitigation

- Container dead: `docker compose up -d postgres` (data is on the `pgdata` volume).
- Out of connections: check `pg_stat_database_numbackends` vs `max_connections`; restart the app pool.
- Disk full: check `df` on the `pgdata` volume; in production, free space or fail over.

## Escalation

Database owner immediately. Postgres outages are never a single-person incident.

## After the incident

Always a correction-of-error review: a database outage breaks every SLO at once.
