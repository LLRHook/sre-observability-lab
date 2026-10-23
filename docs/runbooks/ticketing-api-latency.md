# Runbook: ticketing-api latency SLO burn

Alerts: `TicketingApiLatencyBurnRatePage`, `TicketingApiLatencyBurnRateTicket`.

## What this alert means

The latency SLI is the fraction of customer-facing requests that complete within 500 ms. The
objective is 99% over 30 days, so the error budget is 1% of requests. "Errors" for this SLO are
slow requests (`_count` minus the `le="0.5"` histogram bucket), not failed ones; a request can
be slow *and* fail and count against both SLOs.

Same burn-rate ladder as availability: page at 14.4x/1h+5m or 6x/6h+30m, ticket at 3x/1d+2h or 1x/3d+6h.

## First five minutes

1. Dashboard: http://localhost:3000/d/slo-overview/slo-overview?var-slo=ticketing-api-latency and the golden-signals board.
2. Which quantile moved, and for which route?
   ```promql
   histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket{job="ticketing-api"}[5m])))
   ```
3. Is it the database? Compare with `pg_stat_activity_count` by state and transaction rate on the Postgres dashboard. A rise in `active`/`idle in transaction` backends points at lock contention.
4. Is it injected? `max(chaos_latency_ms)`.
5. Is it saturation? `http_requests_in_flight` climbing with constant traffic means the service is queueing.

## Diagnosis tree

- All routes slow by a constant offset -> injected latency (lab) or a slow dependency on every path.
- `/purchase` slow, `/events` fine -> write path: Postgres locks (`FOR UPDATE` on the events row), disk.
- p50 fine, p99 bad -> tail latency: GC pauses, pool exhaustion, a few slow customers.

## Mitigation

- Chaos (lab): `make chaos-reset` or approve `reset_chaos`.
- Saturation: shed load or scale out; in this lab, lower `LOADGEN_RATE`.
- Lock contention: look at the hottest event row; consider optimistic concurrency.

## Escalation

As for availability. Latency pages are rarer and usually precede availability pages; treat a latency page during a sale as a capacity incident.

## Known noise

- Cold start right after `make up`: the first requests include connection-pool warm-up. The 1h window absorbs this; the 5m window alone cannot page.
