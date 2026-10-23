# Correction of Error: ticketing-api availability burn page (game day, 2026-10-23)

| | |
|---|---|
| Incident | `inc-20261023-124434-1c3fd5`, alert `TicketingApiAvailabilityBurnRatePage` |
| Severity | Page (SLO burn 14.4x over 1h/5m exceeded; measured 297x over 5m) |
| Duration | 12:41:41 UTC fault injected -> 12:47:49 mitigated (6 min 8 s); page cleared 13:17:39, incident closed 13:18:34 |
| Trigger | Deliberate: `make gameday` injected a 50% error rate into `ticketing-api` |
| Author | Victor Ivanov |
| Status | Action items filed; one fixed the same day |

This is a blameless review in the AWS COE format of a planned game-day incident. The fault
was injected on purpose; the point of the review is the response path, not the fault.

## 1. Summary

At 12:41:41 UTC the game-day script set `chaos.error_rate=0.5` on the ticketing API, which
made half of all customer requests return HTTP 500. Prometheus moved the availability page
alert to pending at 12:42:24 and to firing at 12:44:24 (`for: 2m`). Alertmanager routed it to
the pager receiver; the ops-copilot opened an incident at 12:44:34, classified it as an
injected fault with 0.9 confidence from the `chaos_error_rate` gauge, cited burn rates, the
per-route 5xx breakdown and 848 matching error log lines, and proposed `reset_chaos`. The
on-call approved at 12:47:49 and the chaos state was cleared in the same second. The 5m burn
rate returned to 0 within five minutes; the page itself stayed firing until 13:17:39 because
the 6h/30m alert pair holds until the errors leave the 30-minute window. The incident closed
when Alertmanager delivered the resolved notification at 13:18:34.

The run also exposed a defect in the response tooling: the copilot's webhook endpoint
returned HTTP 500 on every delivery after completing its work, so Alertmanager retried ten
times a minute (81 deliveries for one incident in ten minutes). Triage, dedupe, approval and
audit were all correct; only the HTTP status was wrong. It was fixed and regression-tested
the same afternoon.

## 2. Impact

- Customer impact (simulated): ~50% of `/purchase`, `/events` and `/orders/{id}` requests
  failed for 6 min 8 s. At the lab's 9.7 rps (`sum(rate(http_requests_total[5m]))` at 12:46)
  that is about 1,870 failed requests (`sum(increase(http_requests_total{job="ticketing-api",status=~"5.."}[7m]))`
  evaluated at 12:48:30 returned 1,869).
- Error budget: the 30-day availability budget (0.1% of requests, 43.2 minutes of full
  outage) went from 100% remaining to a negative value during the burn; the copilot's note
  recorded `budget remaining=-125.572` at 12:44:34, computed over the ~3.5 hours of history
  Prometheus had, not a full 30 days. In a month-old deployment the same six minutes at 50%
  would have cost about 7% of the monthly budget (3 error-minutes equivalent / 43.2).
- Operational impact: one page, one inhibited ticket, zero duplicate pages. 81 webhook
  deliveries to the copilot instead of 1 (no customer effect; CPU and log noise only).

## 3. Timeline (UTC, from recorded system timestamps)

| Time | T+ | Source | Event |
|---|---|---|---|
| 12:41:41 | 0:00 | gameday | Baseline checked: 9/9 targets up, 5m error ratio 0, budget remaining 1.0 |
| 12:41:41 | 0:00 | gameday | `POST /admin/chaos {"error_rate":0.5}` accepted |
| 12:42:24 | 0:43 | Prometheus | `TicketingApiAvailabilityBurnRatePage` pending (`activeAt` 12:42:24.39); `for: 2m` starts |
| 12:42:24 | 0:43 | Prometheus | `TicketingApiAvailabilityBurnRateTicket` pending (`for: 5m`) |
| 12:44:24 | 2:43 | Prometheus | Page firing; sent to Alertmanager |
| 12:44:24 | 2:43 | Alertmanager | Alert active, receiver `pager`, `group_wait: 10s` starts |
| 12:44:34 | 2:53 | ops-copilot | Webhook received (`alert_received`), context gathered in 97 ms, triage note written (`injected_fault`, 0.9), posted to incident channel |
| 12:44:34 | 2:53 | Alertmanager | Webhook returned 500 (see section 6); first of 10 retries per minute |
| 12:46:41 | 5:00 | Prometheus | `BlackboxProbeFailed` for `/events` pending (probe hit a 500); never fired |
| 12:47:24 | 5:43 | Prometheus | Ticket alert firing |
| 12:47:43 | 6:02 | Alertmanager | Ticket observed `suppressed`, `inhibitedBy` the page's fingerprint `69ee107acaf07c33` |
| 12:47:49 | 6:08 | ops-copilot | `approval_granted` by Victor Ivanov for action 0 (`reset_chaos`), `dry_run=false` |
| 12:47:49 | 6:08 | ticketing-api | `POST /admin/chaos/reset` -> `{"error_rate":0.0,"latency_ms":0,"db_fault":false}`; audit `executed` |
| 12:52:52 | 11:11 | Prometheus | 5m burn rate 0.0; 30m/1h/6h still 153x |
| 12:53:20 | 11:39 | operator | Copilot rebuilt with the webhook fix; next Alertmanager delivery at 12:54:19 returned 200 |
| 13:17:39 | 35:58 | Prometheus | Page no longer firing (`endsAt` 13:17:39.39; last firing sample 13:17:30); 30m window clear of the burn |
| 13:18:34 | 36:53 | ops-copilot | Resolved webhook received (`resolve_timeout`/group timing); incident status `resolved`, channel post `RESOLVED` |

## 4. Metrics

- Detection: 43 s from injection to pending, 2 min 43 s to firing (the `for` clause is 2 min
  of that). The 1h window needed 1.7 minutes at 500x burn to cross 14.4x, as the arithmetic
  predicts (60 min x 14.4 / 500).
- Notification: 10 s from firing to the copilot incident (`group_wait: 10s`).
- Triage: 97 ms from webhook to triage note (rules engine; no LLM call).
- Time to mitigate: 6 min 8 s from injection, 3 min 25 s from page. The human step
  (reading the note and approving) was 3 min 15 s.
- Time to page clear: 29 min 50 s after mitigation; incident closed 55 s after that.
- Burn rates at triage: 5m 297x, 1h 127x, 6h 127x (all three equal because the service had
  only 3.5 hours of history, so every window saw the same fraction).
- Webhook deliveries: 81 for one incident before the fix; 1 per `group_interval` after.

## 5. Detection, diagnosis, mitigation

**How was the problem detected?** By the SLO burn-rate page, 2 min 43 s after injection.
No customer-style signal (the blackbox probe) fired; it went pending five times and was
absorbed by `for: 2m`.

**Could detection have been faster?** Only by shortening `for` or using a shorter long
window, both of which trade noise for speed. Two minutes of settling is acceptable for a
burn that costs 2% of the monthly budget per hour. For an on-sale window a tighter,
time-boxed pair would be justified.

**How was the cause diagnosed?** The copilot's rules engine read `chaos_error_rate=0.5`
from Prometheus and the `injected failure` log messages from OpenSearch and classified the
incident as `injected_fault`. A human reading the triage note had the cause, the per-route
breakdown and the proposed fix in one screen. The runbook's diagnosis tree lists the same
path ("5xx spread across all routes, logs say injected failure -> chaos injection").

**Could diagnosis have been faster?** It was effectively instant. The 3 min 15 s human
step was the operator reading and choosing to approve; a dry-run was not needed because the
blast radius was stated as "chaos state only".

**How was it mitigated?** The approved `reset_chaos` action called the service's admin
endpoint with the copilot's scoped token. The copilot cannot do anything else.

**Could mitigation have been faster?** Auto-approval exists (`--auto-approve`) and would
have saved three minutes, but the lab's position is that nothing executes without a named
human, so no.

**What went wrong in the response tooling?** The webhook 500 (section 6). It did not slow
the response, but in production it would have produced retry storms and alert fatigue in
the receiver's own monitoring.

**Was the "resolved" experience right?** The page stayed up for 30 minutes after the fix.
That is the documented behaviour of the 6h/30m pair, and the on-call could see the 5m burn
at 0 on the dashboard, but a pager that keeps showing "firing" after a confirmed fix invites
people to silence it. Action item 3.

## 6. Five whys: the webhook 500

1. Why did Alertmanager retry the pager webhook 81 times? Because every delivery returned
   HTTP 500.
2. Why did the copilot return 500 after successfully handling the alert? Because the log
   call after the pipeline raised `KeyError: "Attempt to overwrite 'created' in LogRecord"`.
3. Why did the log call raise? The handler passed `extra={**result}` and `result` has a key
   named `created`, which `logging.LogRecord` reserves for the record's creation time.
4. Why was that not caught before the game day? The unit tests exercised `Pipeline` directly
   and never called the HTTP handler; the service logged nothing at INFO during tests.
5. Why did the tests stop at the pipeline boundary? The HTTP layer looked trivial, so there
   was no HTTP-level test. Trivial glue is where reserved-name collisions live.

Contributing factor: Alertmanager's retry policy is correct and loud, but the copilot had
no alert on its own 5xx rate, so the signal was only visible in logs and in a counter
nobody was watching.

## 7. What went well

- The alert ladder behaved exactly as the promtool tests predicted, including the ticket
  being inhibited by the page.
- The copilot's triage note contained everything needed to decide in one read.
- The audit log reconstructs the whole incident with actor, action and timestamps.
- The copilot's own metrics (`copilot_webhooks_total` vs `copilot_incidents_total`) exposed
  the webhook bug without anyone looking for it.

## 8. Action items

| # | Action | Owner | Priority | Due | Status |
|---|---|---|---|---|---|
| 1 | Fix the webhook log call (prefix result keys) and add an HTTP-level regression test | Victor Ivanov | P1 | 2026-10-23 | Done (commit "Fix webhook 500 caused by a LogRecord key collision found on the first game day") |
| 2 | Add a static alert on `copilot` 5xx rate (`http_requests_total{job="ops-copilot",status=~"5.."}`) routed as a ticket, with a runbook | Victor Ivanov | P2 | 2026-10-30 | Open |
| 3 | Decide and document the "resolved" semantics for pages: either annotate the page with the expected clear time per pair, or add a `resolved_early` note to the copilot when the 5m burn is 0 while the page is still firing | Victor Ivanov | P2 | 2026-11-06 | Open |
| 4 | Raise the game-day script's page-clear wait to cover the 30m window (was 600 s) | Victor Ivanov | P3 | 2026-10-23 | Done |
| 5 | Record `BlackboxProbeFailed` pending-only episodes in the noise report as expected during error injection; consider `for: 3m` for probe alerts on routes that share the SLO page | Victor Ivanov | P3 | 2026-11-13 | Open |
| 6 | Extend the test suite's HTTP coverage to `/incidents/{id}/approve` and `/reject` | Victor Ivanov | P3 | 2026-11-13 | Open |

## 9. References

- Timeline source: game-day stdout and `data/copilot/audit.jsonl`, reproduced in
  [EVIDENCE.md](../EVIDENCE.md) section 6.
- Runbook: [ticketing-api-availability](../runbooks/ticketing-api-availability.md).
- Alert definitions: `prometheus/rules/generated/slo_rules.yml`.
