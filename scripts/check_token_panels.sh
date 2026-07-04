#!/usr/bin/env sh
# Verifies the token-spend and cache-hit-ratio panels exist and query the
# wrapper cost/token families. Fails non-zero if a panel is missing.
set -eu
DB="dashboards/task-delivery.json"

python3 -c "import json,sys; json.load(open('$DB'))"  # valid JSON

# $-spend panel: title present and derives $ from operator_task_tokens_total by kind,repo.
jq -e '.panels[] | select(.title=="Cost (USD) by Kind and Repo")
       | .targets[].expr | select(test("operator_task_tokens_total"))
       | select(test("by ?\\(kind, ?repo\\)"))' "$DB" >/dev/null

# cache-hit-ratio panel: title present and computes cache_read/(cache_read+input).
jq -e '.panels[] | select(.title=="Cache Hit Ratio by Kind")
       | .targets[].expr
       | select(test("type=\"cache_read\""))
       | select(test("cache_read\\|input"))' "$DB" >/dev/null

echo "token panels OK"
