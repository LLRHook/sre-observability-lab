# Runbook: BlackboxProbeFailed

Alert: `BlackboxProbeFailed` (severity page, service ticketing-api).

## What this alert means

`blackbox_exporter` performed an HTTP GET against the API from outside the process and did not
get a 2xx for 2 minutes. This is the outside-in view: it catches cases where the process is up
and happily reporting metrics but nobody can reach it (bad listener, broken routing, TLS).

## First five minutes

1. Which URL? `instance` label (`/healthz` or `/events`).
2. Does the server-side SLI agree? If `/events` fails but the availability SLI is clean, the
   failure is between the probe and the service, not in the service.
3. `probe_http_status_code{job="blackbox"}` and `probe_duration_seconds` tell you *how* it failed
   (connection refused -> status 0; 5xx -> the app; timeout -> probe duration at the limit).
4. Try it yourself: `curl -i localhost:8000/events`.

## Mitigation

- App returning 5xx: this is really an availability incident; follow [ticketing-api-availability](ticketing-api-availability.md).
- Connection refused: the container is down; `TargetDown` will fire too and inhibit this alert.

## Escalation

Service owner. If only the probe fails and users are fine, downgrade to a ticket and fix the probe.

## Known noise

- Probe timeouts under injected latency > 5s; the probe `timeout` is 5s in `blackbox/blackbox.yml`.
