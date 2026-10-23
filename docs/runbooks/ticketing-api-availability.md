# Runbook: ticketing-api availability SLO burn

Alerts: `TicketingApiAvailabilityBurnRatePage` (severity page), `TicketingApiAvailabilityBurnRateTicket` (severity ticket).

## What this alert means

The availability SLI (fraction of customer-facing requests that did *not* return a 5xx) is
consuming the 30-day error budget faster than the objective allows:

| Alert | Pairs that trip it | What it implies |
|---|---|---|
| Page | 14.4x over 1h **and** 5m, or 6x over 6h **and** 30m | 2% / 5% of the monthly budget gone in 1h / 6h. A human must look now. |
| Ticket | 3x over 1d **and** 2h, or 1x over 3d **and** 6h | Slow leak. Fix within days, not minutes. |

Objective 99.9% over 30d = 0.1% error budget = 43.2 minutes of total failure per 30 days.
Burn rate 1 = exactly on budget; 14.4 = the whole budget gone in ~2 days.

## First five minutes

1. Open the SLO dashboard: http://localhost:3000/d/slo-overview/slo-overview?var-slo=ticketing-api-availability
2. Which routes are failing?
   ```promql
   sum by (route, status) (rate(http_requests_total{job="ticketing-api",status=~"5.."}[5m]))
   ```
3. Is a dependency the cause? `pg_up`, `max(chaos_db_fault)`, and the Postgres dashboard.
4. Is chaos injection active (lab)? `max(chaos_error_rate)`, `max(chaos_latency_ms)`.
5. Recent error logs (OpenSearch, Grafana Explore):
   ```
   service:ticketing-api AND level:ERROR
   ```
   Group by `msg` to find the dominant failure; pick one `request_id` and follow it.

## Diagnosis tree

- 5xx concentrated on `/purchase` with `database error` logs -> Postgres. Follow [postgres-down](postgres-down.md).
- 5xx spread across all routes, logs say `injected failure` -> chaos injection (lab). Reset it.
- 5xx spread across all routes, no DB signal -> bad deploy or resource exhaustion. Check in-flight gauge, container restarts.
- No 5xx at all but the alert fired -> check `up{job="ticketing-api"}` and the recording rules' freshness (`prometheus_rule_evaluation_failures_total`).

## Mitigation

- Chaos (lab): `make chaos-reset`, or approve the copilot's `reset_chaos` proposal.
- Real deploy regression: roll back first, diagnose second.
- Dependency outage: fail over / restart the dependency; the SLO alert is inhibited while `PostgresDown` fires.

## Escalation

- Page -> primary on-call owns it. If not mitigated in 30 minutes, pull in the service owner.
- Ticket -> service owner's queue; discuss at the weekly SLO review. If a ticket-level burn persists for 3 days, it becomes a page by construction (the 3d/6h pair at 1x).

## Known noise

- Loadgen restarts cause a short blip in both windows; it should never satisfy the 1h window. If it does, the loadgen is misbehaving, not the alert.
- A page also opens a ticket-level burn; the ticket is inhibited while the page is firing (see `alertmanager/alertmanager.yml`).

## After the incident

Open a correction-of-error review (`docs/coe/`) if the budget consumed exceeded 5%, or if detection, diagnosis or mitigation took longer than it should have.
