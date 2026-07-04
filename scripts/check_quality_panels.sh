#!/usr/bin/env sh
# Verifies the quality-feedback dashboard's three model-keyed ratio panels exist
# and query the G4 quality-proxy metric families. Fails non-zero if a panel is
# missing or the PromQL drifts from the expected shape.
set -eu
DB="dashboards/quality-feedback.json"

python3 -c "import json,sys; json.load(open('$DB'))"  # valid JSON

# Review find-rate by model: changes_requested / all outcomes, grouped by model.
jq -e '.panels[] | select(.title=="Review Find-Rate by Model")
       | .targets[].expr | select(test("operator_review_outcome_total"))
       | select(test("verdict=\"changes_requested\""))
       | select(test("by ?\\(model\\)"))' "$DB" >/dev/null

# Findings per review by model: findings sum / outcome count, grouped by model.
jq -e '.panels[] | select(.title=="Findings per Review by Model")
       | .targets[].expr | select(test("operator_review_findings_total"))
       | select(test("operator_review_outcome_total"))
       | select(test("by ?\\(model\\)"))' "$DB" >/dev/null

# Implement CI-pass-rate by model: pass / all conclusions, grouped by model.
jq -e '.panels[] | select(.title=="Implement CI Pass-Rate by Model")
       | .targets[].expr | select(test("operator_implement_ci_total"))
       | select(test("result=\"pass\""))
       | select(test("by ?\\(model\\)"))' "$DB" >/dev/null

echo "quality panels OK"
