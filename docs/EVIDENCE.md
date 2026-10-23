# Runtime evidence

Captured 2026-10-23 on macOS (Darwin 25.6.0), Docker Desktop 29.7.2, uv 0.10.2, Python 3.13.
Everything below is pasted from real command output on that day; nothing is asserted from
reading code. Component versions: Prometheus 3.15.1, Alertmanager 0.34.1, Grafana 13.2.4,
grafana-opensearch-datasource 2.34.6, OpenSearch 3.9.0, Fluent Bit 5.1.5, Postgres 18.6,
postgres_exporter 0.20.1, blackbox_exporter 0.29.1.

## 1. Quality gate

```
$ uv run pytest -q
...............................................................          [100%]
63 passed in 0.33s

$ uv run ruff check .
All checks passed!

$ uv run yamllint -s . ; echo exit=$?
exit=0

$ make promtool
  SUCCESS: /p/prometheus.yml is valid prometheus config file syntax
Checking /p/rules/generated/slo_rules.yml
  SUCCESS: 42 rules found
Checking /p/rules/static/platform.yml
  SUCCESS: 4 rules found
promtool test rules slo_alerts.test.yml platform_alerts.test.yml
  SUCCESS
  SUCCESS

$ bash alertmanager/tests/routes_test.sh
config ok
ok   alertname=TicketingApiAvailabilityBurnRatePage severity=page service=ticketing-api slo=ticketing-api-availability -> pager
ok   alertname=TicketingApiLatencyBurnRatePage severity=page service=ticketing-api slo=ticketing-api-latency -> pager
ok   alertname=TicketingApiAvailabilityBurnRateTicket severity=ticket service=ticketing-api slo=ticketing-api-availability -> ticket-queue
ok   alertname=PostgresDown severity=page service=postgres -> pager
ok   alertname=TargetDown severity=page service=ticketing-api -> pager
ok   alertname=PrometheusRuleEvaluationFailures severity=ticket service=prometheus -> ticket-queue
ok   alertname=SomethingNew severity=warning service=ticketing-api -> ticket-queue
ok   alertname=SomethingElse -> ticket-queue
routes: 8 passed, 0 failed
```

The 63 tests break down as: 11 copilot pipeline, 11 copilot guardrails (adversarial), 12
copilot eval (10 recorded incidents + coverage + summary), 3 copilot HTTP, 15 SLO generator
(including parametrised spec-validation cases), 8 ticketing API, 3 noise report.

## 2. Stack up

```
$ docker compose ps --format 'table {{.Name}}\t{{.Status}}'      (11 services; opensearch-init exits 0 by design)

$ curl -s localhost:9090/api/v1/targets | ... (job, instance, health)
alertmanager    alertmanager:9093                       up
blackbox        http://ticketing-api:8000/healthz       up
blackbox        http://ticketing-api:8000/events        up
fluent-bit      fluent-bit:2020                         up
loadgen         loadgen:8002                            up
ops-copilot     ops-copilot:8001                        up
postgres        postgres-exporter:9187                  up
prometheus      localhost:9090                          up
ticketing-api   ticketing-api:8000                      up

$ curl -s localhost:9090/api/v1/rules | ...
groups 5 rules 46 errors 0

$ curl -s localhost:9093/api/v2/status | ...
alertmanager 0.34.1 ready

$ curl -s localhost:8001/healthz
{"status":"ok","summarizer":"rules"}
```

No `ANTHROPIC_API_KEY` was present, so every triage note below came from the rules engine.
The LLM path is covered by unit tests with a scripted model only.

## 3. Memory footprint

```
$ docker stats --no-stream   (steady state, loadgen at 10 rps)
srelab-loadgen-1             28.93MiB / 128MiB
srelab-ticketing-api-1       53.14MiB / 192MiB
srelab-grafana-1             321.4MiB / 512MiB
srelab-ops-copilot-1         48.96MiB / 256MiB
srelab-postgres-1            33.51MiB / 256MiB
srelab-opensearch-1          1.011GiB / 1.5GiB
srelab-fluent-bit-1          67.53MiB / 96MiB
srelab-postgres-exporter-1   35.14MiB / 64MiB
srelab-prometheus-1          159.7MiB / 512MiB
srelab-alertmanager-1        70.48MiB / 128MiB
srelab-blackbox-exporter-1   49.91MiB / 64MiB
```

Total about 1.9 GiB, of which OpenSearch (512 MB heap plus off-heap) is more than half.

## 4. Logging pipeline

```
$ curl -s 'localhost:9200/_cat/indices/srelab-logs-*?v&h=index,docs.count,store.size,health'
index                  docs.count store.size health
srelab-logs-2026.10.23      18512      4.7mb green

$ curl -s 'localhost:9200/srelab-logs-*/_count'
{"count":18512,"_shards":{"total":1,"successful":1,"skipped":0,"failed":0}}

aggregation by service: ticketing-api 18270, ops-copilot 178, loadgen 54
aggregation by level:   INFO 16531, ERROR 1906, WARNING 65

$ curl -s -X POST 'localhost:9200/srelab-logs-*/_search?size=1' -d '{"query":{"bool":{"filter":[{"term":{"level":"ERROR"}},{"term":{"event":"chaos_error"}}]}},"sort":[{"@timestamp":"desc"}]}'
{
 "@timestamp": "2026-10-23T12:47:49.747Z",
 "ts": "2026-10-23T12:47:49.746Z",
 "level": "ERROR",
 "logger": "ticketing-api",
 "service": "ticketing-api",
 "request_id": "c1c1a3acee4648ee",
 "msg": "injected failure",
 "event": "chaos_error",
 "error_rate": 0.5,
 "container": "/srelab-ticketing-api-1",
 "source": "stdout"
}

$ curl -s localhost:9200/_plugins/_ism/policies/srelab-logs-retention | ...
policy srelab-logs-retention ['hot', 'delete']
$ curl -s localhost:9200/_index_template/srelab-logs | ...
template srelab-logs ['srelab-logs-*']
```

The sample document shows the Docker fluentd driver -> Fluent Bit -> OpenSearch path working
on Docker Desktop for Mac: the JSON line was parsed into fields, `container_name` renamed to
`container`, and the `@timestamp` set by the log driver.

## 5. Grafana

```
$ curl -s localhost:3000/api/health
{"database": "ok", "version": "13.2.4"}

$ curl -s 'localhost:3000/api/search?type=dash-db'      (uid | title | folder)
postgres        | Postgres                | SRE Lab
golden-signals  | Service golden signals  | SRE Lab
slo-overview    | SLO overview            | SRE Lab

$ curl -s localhost:3000/api/datasources                 (uid type)
alertmanager  alertmanager
opensearch    grafana-opensearch-datasource
prometheus    prometheus

$ curl -s -u admin:admin localhost:3000/api/datasources/uid/opensearch/health
{"message":"Index OK. Time field name OK.","status":"OK"}
```

## 6. Game day, first run (12:41 to 12:48 UTC): found a bug

`make gameday APPROVER="Victor Ivanov"` with the default 50% error injection.

```
[    0.0s] baseline    all targets up; 5m error ratio=0.0; 30d budget remaining=1.0
[    0.0s] inject      chaos set: {'error_rate': 0.5, 'latency_ms': 0, 'db_fault': False}
[   45.1s] detect      TicketingApiAvailabilityBurnRatePage pending in Prometheus (for: 2m starts)
[  165.4s] page        TicketingApiAvailabilityBurnRatePage FIRING in Prometheus
[  165.4s] route       Alertmanager has it: receivers=['pager'] state=active
[  175.4s] triage      incident inc-20261023-124434-1c3fd5 [rules] classification=injected_fault confidence=0.9 actions=['reset_chaos', 'acknowledge']
[  175.4s] triage.evidence  [prometheus] burn rate 5m=297.3x, 1h=126.6x, 6h=126.6x; budget remaining=-125.572
[  175.4s] triage.evidence  [prometheus] 5xx by route (5m): /purchase 500 1.467/s, /events 500 0.837/s, /orders/{order_id} 500 0.474/s
[  175.4s] triage.evidence  [prometheus] p99 latency 0.008s
[  175.4s] triage.evidence  [opensearch] 848 error/warn log lines in 15m: injected failure x50
[  175.4s] triage.evidence  [runbook] runbook ticketing-api-availability.md consulted
>>> Waiting for a human. In another shell run:
>>>   make approve INCIDENT=inc-20261023-124434-1c3fd5 ACTION=0 APPROVER="Victor Ivanov"
[  368.2s] approval    approved by Victor Ivanov; executed reset_chaos -> {'error_rate': 0.0, 'latency_ms': 0, 'db_fault': False}
[  368.2s] mitigate    chaos state now: {'error_rate': 0.0, 'latency_ms': 0, 'db_fault': False}
```

Inhibition, observed while the page was active (12:47:43 UTC, after the ticket's `for: 5m`):

```
$ curl -s localhost:9093/api/v2/alerts | ... (alertname severity state inhibitedBy receivers fingerprint)
TicketingApiAvailabilityBurnRateTicket ticket suppressed inhibitedBy=['69ee107acaf07c33'] receivers=['ticket-queue'] fp=58419f6587aa9f87
TicketingApiAvailabilityBurnRatePage   page   active     inhibitedBy=[]                   receivers=['pager']        fp=69ee107acaf07c33
```

Audit log for the incident (`data/copilot/audit.jsonl`, dedupe entries omitted):

```
12:44:34.469 inc-20261023-124434-1c3fd5 alert_received    alertmanager  {"receiver":"pager","alertname":"TicketingApiAvailabilityBurnRatePage","fingerprint":"69ee107acaf07c33",...}
12:44:34.566 inc-20261023-124434-1c3fd5 context_gathered  copilot       {"metric_keys":["burn_rate_1h","burn_rate_5m","burn_rate_6h","chaos_db_fault","chaos_error_rate",...],"log_hits":848,"runbook_found":true}
12:44:34.568 inc-20261023-124434-1c3fd5 triage_note       copilot       {"summarizer":"rules","classification":"injected_fault","confidence":0.9,"proposed_actions":["reset_chaos","acknowledge"]}
12:47:49.834 inc-20261023-124434-1c3fd5 approval_granted  Victor Ivanov {"action":"reset_chaos","params":{},"dry_run":false,"action_index":0}
12:47:49.848 inc-20261023-124434-1c3fd5 executed          copilot       {"action":"reset_chaos","outcome":{"dry_run":false,"plan":{...,"blast_radius":"ticketing-api chaos state only (no data, no restarts)"},"result":{"error_rate":0.0,...}}}
```

**What the run found.** The copilot's metrics showed `copilot_webhooks_total{receiver="pager"} 81`
against `copilot_incidents_total{outcome="created"} 1` and `deduped 80` within ten minutes,
far more deliveries than `group_interval: 1m` can explain. Alertmanager's log had the cause:

```
level=ERROR source=dispatch.go:589 msg="Notify for alerts failed" ... err="pager/webhook[0]: notify retry canceled after 10 attempts: unexpected status code 500: Internal Server Error"
```

and the copilot's log had the traceback: `KeyError: "Attempt to overwrite 'created' in
LogRecord"`. The handler logged `extra={..., **result}` and `result` contains a key named
`created`, which `logging.LogRecord` already defines. The pipeline had already created or
deduped the incident, so the behaviour was correct and the audit log was right; only the HTTP
response was wrong, and Alertmanager retried ten times a minute. Fixed in commit
`Fix webhook 500 caused by a LogRecord key collision found on the first game day` with an
HTTP-level regression test (`tests/test_copilot_api.py`). After the rebuild:

```
{"ts": "2026-10-23T12:54:19.744Z", "level": "INFO", "service": "ops-copilot", "msg": "webhook handled", "event": "webhook", "receiver": "pager", "incidents_created": [], "incidents_deduped": ["inc-20261023-124434-1c3fd5"], "incidents_resolved": []}
Alertmanager "Notify for alerts failed" in the following 60 s: 0
copilot_webhooks_total{receiver="pager",status="firing"} 1.0   (counter reset by the restart)
```

The first run's script was stopped before it saw resolution because it waited only 600 s
for the page to clear; the 6h/30m pair legitimately holds the page for ~30 minutes after
mitigation (see DEEP_DIVE, "Multi-window, multi-burn-rate alerting"). Resolution was
captured by hand instead:

```
13:17:39.392Z  Prometheus endsAt for TicketingApiAvailabilityBurnRatePage (last firing sample in ALERTS at 13:17:30)
13:18:34.532Z  audit: {"incident_id": "inc-20261023-124434-1c3fd5", "step": "resolved", "actor": "alertmanager", "details": {"ends_at": "2026-10-23T13:17:39.392Z"}}
13:18:34.534Z  copilot log: "webhook handled" ... "incidents_resolved": ["inc-20261023-124434-1c3fd5"]
incident-channel.log: RESOLVED TicketingApiAvailabilityBurnRatePage (inc-20261023-124434-1c3fd5) at 2026-10-23T13:17:39.392Z
```

The wait in `tools/gameday.py` was raised to 2700 s and the run repeated below.

Alert noise report over the first run's window:

```
$ uv run python tools/alert_noise_report.py --window 3h
alert                                            severity  firings  flaps  pending_only  total_firing_s
TicketingApiAvailabilityBurnRatePage{...}        page      1        0      0             765 (still firing at report time)
TicketingApiAvailabilityBurnRateTicket{...}      ticket    1        0      0             585
BlackboxProbeFailed{... /events}                 page      0        0      5             0
BlackboxProbeFailed{... /healthz}                page      0        0      1             0
PostgresDown{...}                                page      0        0      1             0
TargetDown{... loadgen | ops-copilot | ticketing-api}  page  0     0      1 each         0
Recommendations:
  - BlackboxProbeFailed{... /events}: pending 5x but never fired - the `for` clause is absorbing noise.
```

The `/events` probe went pending five times during the 50% error injection (each probe has
a 50% chance of a 500) and never fired because two consecutive failed probes within `for: 2m`
did not happen. The single pending episodes for PostgresDown and TargetDown are from stack
start-up, before the exporters were ready.

## 7. Game day, second run (clean, after the fix)

`make gameday APPROVER="Victor Ivanov"`, started 13:19:00 UTC, 80 s after the first run's
page resolved (so the 1h/6h windows still carried the first burn: budget remaining -67.9 at
baseline). Full report with triage note and audit trail: `docs/gameday/20261023-131900.md`.

```
[    0.0s] baseline    all targets up; 5m error ratio=0.0; 30d budget remaining=-67.9
[    0.0s] inject      chaos set: {'error_rate': 0.5, 'latency_ms': 0, 'db_fault': False}
[   55.2s] detect      TicketingApiAvailabilityBurnRatePage pending in Prometheus (for: 2m starts)
[  175.4s] page        TicketingApiAvailabilityBurnRatePage FIRING in Prometheus
[  175.4s] route       Alertmanager has it: receivers=['pager'] state=active
[  185.4s] triage      incident inc-20261023-132204-e12f4d [rules] classification=injected_fault confidence=0.9 actions=['reset_chaos', 'acknowledge']
[  185.4s] triage.evidence  [prometheus] burn rate 5m=289.9x, 1h=92.4x, 6h=92.4x; budget remaining=-91.388
[  185.4s] triage.evidence  [prometheus] 5xx by route (5m): /purchase 500 1.603/s, /events 500 1.0/s, /orders/{order_id} 500 0.435/s
[  185.4s] triage.evidence  [prometheus] p99 latency 0.008s
[  185.4s] triage.evidence  [opensearch] 867 error/warn log lines in 15m: injected failure x50
[  185.4s] triage.evidence  [runbook] runbook ticketing-api-availability.md consulted
>>> Waiting for a human. In another shell run:
>>>   make approve INCIDENT=inc-20261023-132204-e12f4d ACTION=0 APPROVER="Victor Ivanov"
[  200.5s] approval    approved by Victor Ivanov; executed reset_chaos -> {'error_rate': 0.0, 'latency_ms': 0, 'db_fault': False}
[  200.5s] mitigate    chaos state now: {'error_rate': 0.0, 'latency_ms': 0, 'db_fault': False}
[  500.9s] recover     5m burn rate below page threshold
>>> Waiting for the page to clear; the 30m short window makes this ~30 min.
[ 2012.5s] resolve     TicketingApiAvailabilityBurnRatePage no longer firing in Prometheus
[ 2052.6s] resolve     incident inc-20261023-132204-e12f4d resolved via Alertmanager resolved webhook
[ 2052.6s] note        slow-burn ticket alert state=active inhibitedBy=[]
wrote docs/gameday/20261023-131900.md
```

Absolute times: inject 13:19:00, pending 13:19:55, firing 13:21:55, incident 13:22:04,
approval 13:22:18, 5m burn below threshold 13:27:20, page `endsAt` 13:52:09, incident
resolved 13:53:04. Time to mitigate from injection: 3 min 18 s (the human step took 13 s this
time because the operator was waiting for the prompt). Page clear after mitigation: 29 min 51 s,
matching the first run to the second.

Alertmanager at 13:22:17, before approval (the ticket from the first burn re-inhibited by
the new page):

```
TicketingApiAvailabilityBurnRateTicket ticket suppressed inhibitedBy=['69ee107acaf07c33'] receivers=['ticket-queue']
TicketingApiAvailabilityBurnRatePage   page   active     inhibitedBy=[]                   receivers=['pager']
```

The ticket-queue route was exercised too: between the first page resolving (13:17:39) and
the second firing (13:21:55) the ticket alert was no longer inhibited, Alertmanager delivered
it to `ticket-queue` after its 2-minute `group_wait`, and the copilot opened a second incident:

```
{"ts": "2026-10-23T13:19:39.469Z", ..., "msg": "webhook handled", "receiver": "ticket-queue", "incidents_created": ["inc-20261023-131939-0153d8"], ...}
inc-20261023-131939-0153d8 TicketingApiAvailabilityBurnRateTicket receiver=ticket-queue triaged injected_fault ['reset_chaos', 'acknowledge']
```

(At 13:19:39 the second injection had been active for 39 s, so `injected_fault` with
`reset_chaos` was the right call for that incident as well.)

Copilot counters at the end of the day (counters reset at the 12:53 rebuild), and the
webhook health after the fix:

```
copilot_webhooks_total{receiver="pager",status="firing"} 2.0
copilot_webhooks_total{receiver="pager",status="resolved"} 2.0
copilot_webhooks_total{receiver="ticket-queue",status="firing"} 1.0
copilot_incidents_total{outcome="created"} 2.0
copilot_incidents_total{outcome="deduped"} 1.0
copilot_incidents_total{outcome="resolved"} 2.0
copilot_approvals_total{action="reset_chaos",decision="approved",dry_run="false"} 1.0

docker logs srelab-ops-copilot-1 | grep -c "Exception in ASGI"   -> 0 (since the rebuild)
Alertmanager notify failures in the last 60 min: 1, at 12:54:02 "dial tcp ...:8001: connection refused"
  (the copilot container restarting for the fix); none after.
```

Two firing deliveries for two incidents (plus one dedupe when the first run's group was
re-sent after the rebuild) is what `group_wait`/`group_interval` should produce.

Alert noise report over both runs:

```
$ uv run python tools/alert_noise_report.py --window 2h
alert                                              severity  firings  flaps  pending_only  total_firing_s  median_ttr_s  max_ttr_s
TicketingApiAvailabilityBurnRatePage{...}          page      2        0      0             3810            1905          1995
TicketingApiAvailabilityBurnRateTicket{...}        ticket    1        0      0             3975            3975          3975 (still firing; 3d/6h pair)
BlackboxProbeFailed{... /events}                   page      0        0      8             0               0             0
BlackboxProbeFailed{... /healthz}                  page      0        0      1             0               0             0
PostgresDown / TargetDown (3)                      page      0        0      1 each        0               0             0
Recommendations:
  - BlackboxProbeFailed{... /events}: pending 8x but never fired - the `for` clause is absorbing noise.
```

OpenSearch at the end of the day: 74,765 documents in `srelab-logs-2026.10.23`.

## 8. Not verified

- The Claude (LLM) summarizer path: no API key was available. It is covered by unit tests
  with a scripted model (`tests/test_copilot_guardrails.py`) and shares every downstream step
  (validation, approval, audit) with the rules path that was exercised above.
- Long-term behaviour of the ISM policy (the delete state fires after 3 days; the policy
  was installed and attached, not observed deleting).
- The CI workflow was not run on GitHub (no remote); every step it runs was run locally via
  `make test` with the same commands.
