#!/bin/sh
# Idempotent OpenSearch bootstrap: wait for the node, install the index template
# and the ISM retention policy. Runs as a one-shot compose service before Fluent
# Bit starts, so the first log index is created with the right mappings.
set -eu
OS="${OPENSEARCH_URL:-http://opensearch:9200}"

echo "waiting for $OS"
for i in $(seq 1 60); do
  if curl -sf "$OS/_cluster/health?wait_for_status=yellow&timeout=5s" >/dev/null; then
    break
  fi
  sleep 2
done
curl -sf "$OS/_cluster/health" | head -c 300; echo

echo "installing index template srelab-logs"
curl -sf -X PUT "$OS/_index_template/srelab-logs" \
  -H 'Content-Type: application/json' --data-binary @/bootstrap/index-template.json; echo

echo "installing ISM policy srelab-logs-retention"
# PUT without seq_no fails if the policy exists; fetch the version and update in place.
if meta=$(curl -sf "$OS/_plugins/_ism/policies/srelab-logs-retention"); then
  seq=$(echo "$meta" | sed -n 's/.*"_seq_no":\([0-9]*\).*/\1/p')
  pri=$(echo "$meta" | sed -n 's/.*"_primary_term":\([0-9]*\).*/\1/p')
  curl -sf -X PUT "$OS/_plugins/_ism/policies/srelab-logs-retention?if_seq_no=$seq&if_primary_term=$pri" \
    -H 'Content-Type: application/json' --data-binary @/bootstrap/ism-policy.json | head -c 200
else
  curl -sf -X PUT "$OS/_plugins/_ism/policies/srelab-logs-retention" \
    -H 'Content-Type: application/json' --data-binary @/bootstrap/ism-policy.json | head -c 200
fi
echo
echo "opensearch bootstrap complete"
