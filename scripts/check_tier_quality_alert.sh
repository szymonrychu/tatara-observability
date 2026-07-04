#!/usr/bin/env sh
# Verifies the G5 tier-quality rubber-stamp alert rule exists in
# alerts/tatara-quality.yaml with the label set + PromQL shape the design
# requires (homelab/system/tatara_tier_quality/kind/model/project labels,
# find-rate expression scoped to claude-sonnet-5, min-volume gate). Fails
# non-zero if the rule is missing, mislabeled, or the expression drifts.
set -eu
RULES="alerts/tatara-quality.yaml"

python3 - "$RULES" <<'PY'
import sys
import yaml

path = sys.argv[1]
data = yaml.safe_load(open(path))
rules = {r["name"]: r for r in (data.get("rules") or [])}

name = "Tier-quality rubber-stamp (model=claude-sonnet-5)"
assert name in rules, f"missing rule {name!r}"
rule = rules[name]

expr = "\n".join(q["expression"] for q in rule["queries"])
assert "operator_review_outcome_total" in expr, "expr must read operator_review_outcome_total"
assert 'verdict="changes_requested"' in expr, "expr must select changes_requested verdicts"
assert 'model="claude-sonnet-5"' in expr, "expr must scope to claude-sonnet-5"
assert "and on()" in expr, "expr must gate on a minimum review volume (and on() ...)"

labels = rule.get("labels") or {}
expected_labels = {
    "homelab": "true",
    "system": "tatara",
    "tatara_tier_quality": "true",
    "kind": "review",
    "model": "claude-sonnet-5",
    "project": "tatara",
}
for k, v in expected_labels.items():
    assert labels.get(k) == v, f"label {k}={labels.get(k)!r}, want {v!r}"

print("tier-quality alert rule OK")
PY
