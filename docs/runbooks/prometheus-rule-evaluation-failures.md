# Runbook: PrometheusRuleEvaluationFailures

Alert: `PrometheusRuleEvaluationFailures` (severity ticket, service prometheus).

## What this alert means

A rule group failed to evaluate in the last 10 minutes. Failed evaluations mean missing
recording-rule samples, which means burn-rate alerts can be silently absent. This is a
meta-alert about the monitoring itself.

## First five minutes

1. http://localhost:9090/rules shows each group's last error.
2. Usual causes: a rule references a metric with a changed name; a `vector()` label clash; a
   rule file edited by hand instead of regenerated (`make rules`).
3. `promtool check rules prometheus/rules/generated/slo_rules.yml prometheus/rules/static/platform.yml` on the host.

## Mitigation

Fix the rule in `slo/slos.yaml` (never in the generated file), `make rules`, `make promtool`,
then `curl -X POST localhost:9090/-/reload`.

## Escalation

Observability owner. Low urgency unless an SLO alert is known to be missing during an incident.
