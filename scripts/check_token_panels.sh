#!/usr/bin/env sh
# Verifies the token-spend and cache-hit-ratio panels exist and query the
# wrapper cost/token families. Fails non-zero if a panel is missing.
set -eu
DB="dashboards/task-delivery.json"

python3 -c "import json,sys; json.load(open('$DB'))"  # valid JSON

# $-spend panel: title present, derives $ from operator_task_tokens_total by kind,repo,
# and is model+cache-accurate (not the crude kind-coupled expr).
jq -e '.panels[] | select(.title=="Cost (USD) by Kind and Repo")
       | .targets[].expr | select(test("operator_task_tokens_total"))
       | select(test("by ?\\(kind, ?repo\\)"))
       | select(test("model="))
       | select(test("cache_creation"))' "$DB" >/dev/null

# cache-hit-ratio panel: title present, sourced from the durable operator metric
# (NOT the dead ccw_ push), and computes cache_read/(cache_read+input).
jq -e '.panels[] | select(.title=="Cache Hit Ratio by Kind")
       | .targets[].expr
       | select(test("operator_task_tokens_total"))
       | select(test("ccw_") | not)
       | select(test("type=\"cache_read\""))
       | select(test("cache_read\\|input"))' "$DB" >/dev/null

# churn-cost panel: title present, sourced from the terminal-outcome metric, grouped by outcome.
jq -e '.panels[] | select(.title=="Churn Cost (USD) by Outcome")
       | .targets[].expr
       | select(test("operator_task_terminal_tokens_total"))
       | select(test("by ?\\(outcome\\)"))' "$DB" >/dev/null

echo "token panels OK"
