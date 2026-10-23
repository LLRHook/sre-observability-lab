#!/usr/bin/env bash
# Routing assertions with `amtool config routes test`.
# Each case: expected receiver, then the alert's labels. amtool exits non-zero
# when the alert would not be routed to --verify.receivers.
#
# Usage: alertmanager/tests/routes_test.sh [amtool command]
#   default amtool command runs inside the pinned prom/alertmanager image.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$(cd "$HERE/.." && pwd)"
IMAGE="${ALERTMANAGER_IMAGE:-prom/alertmanager:v0.34.1}"

if [[ $# -gt 0 ]]; then
  AMTOOL=("$@")
  CONFIG="$CONFIG_DIR/alertmanager.yml"
else
  AMTOOL=(docker run --rm -v "$CONFIG_DIR:/etc/alertmanager:ro" --entrypoint amtool "$IMAGE")
  CONFIG=/etc/alertmanager/alertmanager.yml
fi

pass=0; fail=0
check() {
  local expected="$1"; shift
  if out=$("${AMTOOL[@]}" config routes test --config.file="$CONFIG" --verify.receivers="$expected" "$@" 2>&1); then
    echo "ok   $* -> $expected"; pass=$((pass+1))
  else
    echo "FAIL $* -> expected $expected"; echo "$out"; fail=$((fail+1))
  fi
}

"${AMTOOL[@]}" check-config "$CONFIG" >/dev/null
echo "config ok"

check pager        alertname=TicketingApiAvailabilityBurnRatePage severity=page service=ticketing-api slo=ticketing-api-availability
check pager        alertname=TicketingApiLatencyBurnRatePage severity=page service=ticketing-api slo=ticketing-api-latency
check ticket-queue alertname=TicketingApiAvailabilityBurnRateTicket severity=ticket service=ticketing-api slo=ticketing-api-availability
check pager        alertname=PostgresDown severity=page service=postgres
check pager        alertname=TargetDown severity=page service=ticketing-api
check ticket-queue alertname=PrometheusRuleEvaluationFailures severity=ticket service=prometheus
# Unknown severity must fall through to the quiet default, never to the pager.
check ticket-queue alertname=SomethingNew severity=warning service=ticketing-api
check ticket-queue alertname=SomethingElse

echo "routes: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
