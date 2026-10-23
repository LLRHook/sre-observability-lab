# Runbook: TargetDown

Alert: `TargetDown` (severity page, `service` = the scrape job name).

## What this alert means

Prometheus could not scrape the target for 2 minutes (`up == 0`). For `ticketing-api` this
means the SLI has no data at all: a burn-rate alert cannot fire, which is exactly why this
alert exists and why it inhibits the derived SLO and probe alerts for the same service.

## First five minutes

1. Which job/instance? The alert labels say. `make targets` shows the whole list.
2. Container state: `docker compose ps`, `docker compose logs --tail=50 <service>`.
3. Can Prometheus reach it? From the Prometheus container: the "Targets" page shows the last scrape error (connection refused vs timeout vs HTTP 500).
4. Is the service up but `/metrics` broken? `curl -s localhost:8000/metrics | head`.

## Mitigation

- Crashed: restart it (`make restart SERVICE=ticketing-api`).
- Metrics endpoint broken by a deploy: roll back; the service may be serving traffic fine.
- Prometheus-side DNS/network: restart Prometheus last, not first.

## Escalation

Service owner. If several targets go down together, suspect the host/network, not the services.

## Known noise

- Right after `make up`, Prometheus may scrape before a container is healthy; the `for: 2m` absorbs this.
- `loadgen` or `ops-copilot` going down is not customer impact; consider severity ticket for those jobs if this pages too often (see `tools/alert_noise_report.py`).
