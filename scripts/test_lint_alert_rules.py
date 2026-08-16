#!/usr/bin/env python3
"""Tests for lint_alert_rules. Run: python3 -m unittest scripts.test_lint_alert_rules
or, from the scripts/ dir: python3 -m unittest test_lint_alert_rules."""

import json
import pathlib
import tempfile
import unittest

import yaml

import lint_alert_rules as lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ALERTS_DIR = REPO_ROOT / "alerts"
DASHBOARDS_DIR = REPO_ROOT / "dashboards"


def _write(tmp: pathlib.Path, body: str) -> str:
    p = tmp / "rules.yaml"
    p.write_text(body)
    return str(p)


class LintRuleLogic(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self, body: str):
        return lint.lint_file(_write(self.tmp, body))

    def test_consumer_side_filter_passes(self):
        body = """
rules:
  - name: "Wrapper HTTP 5xx responses"
    queries:
      - expression: |
          sum(rate(ccw_http_requests_total{status_code=~"5..", route!~"/readyz|/healthz|/metrics"}[10m]))
    threshold: 0
"""
        self.assertEqual(self._violations(body), [])

    def test_justify_annotation_passes(self):
        body = """
rules:
  - name: "Chat HTTP 5xx ratio"
    queries:
      - expression: |
          sum(rate(http_requests_total{job="tatara-chat",status=~"5.."}[5m]))
    threshold: 0.05
    annotations:
      summary: "..."
      tatara_probe_exclusion: "producer-excluded, see router.go:30-34"
"""
        self.assertEqual(self._violations(body), [])

    def test_no_filter_no_justify_is_violation(self):
        body = """
rules:
  - name: "Naked 5xx rule"
    queries:
      - expression: |
          sum(rate(http_requests_total{job="x",status=~"5.."}[5m]))
    threshold: 0.05
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "Naked 5xx rule")

    def test_empty_justify_annotation_is_violation(self):
        body = """
rules:
  - name: "Blank annotation"
    queries:
      - expression: |
          sum(rate(http_requests_total{status=~"5.."}[5m]))
    threshold: 0.05
    annotations:
      tatara_probe_exclusion: "   "
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_named_server_error_statuses_flagged(self):
        body = """
rules:
  - name: "Memory 5xx ratio"
    queries:
      - expression: |
          sum(rate(http_requests_total{pod=~"mem-.+",status=~"Internal Server Error|Service Unavailable"}[10m]))
          /
          clamp_min(sum(rate(http_requests_total{pod=~"mem-.+"}[10m])), 0.001)
    threshold: 0.05
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_non_5xx_http_rule_ignored(self):
        body = """
rules:
  - name: "4xx ratio (not server error)"
    queries:
      - expression: |
          sum(rate(http_requests_total{status=~"4.."}[5m]))
    threshold: 0.05
"""
        self.assertEqual(self._violations(body), [])

    def test_non_http_metric_ignored(self):
        body = """
rules:
  - name: "Operator reconcile errors (result taxonomy)"
    queries:
      - expression: |
          sum(rate(operator_reconcile_total{result="error"}[10m]))
    threshold: 0
"""
        self.assertEqual(self._violations(body), [])

    def test_divisor_without_status_does_not_self_trigger(self):
        # The ratio denominator selects no status; only the numerator 5xx counts,
        # and that numerator carries a route exclusion -> passes.
        body = """
rules:
  - name: "ratio with filtered numerator"
    queries:
      - expression: |
          sum(rate(http_requests_total{status=~"5..", route!~"/readyz|/healthz"}[5m]))
          / clamp_min(sum(rate(http_requests_total{}[5m])), 1)
    threshold: 0.05
"""
        self.assertEqual(self._violations(body), [])


class FabricatedZeroDeadman(unittest.TestCase):
    """Check 1: `or vector(0)` + a `<` threshold + a foreign exporter's metric
    fabricates a zero and pages for the wrong system (tatara-observability#67)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self, body: str):
        return lint.lint_file(_write(self.tmp, body))

    def test_kube_metric_with_vector_zero_and_lt_is_violation(self):
        body = """
rules:
  - name: "Operator pod not ready"
    queries:
      - expression: |
          sum(kube_pod_status_ready{namespace="tatara",condition="true"}) or vector(0)
    math_operator: "<"
    threshold: 1
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "Operator pod not ready")

    def test_lte_operator_is_also_a_violation(self):
        body = """
rules:
  - name: "lte variant"
    queries:
      - expression: |
          sum(kube_pod_status_ready{namespace="tatara"}) or vector(0)
    math_operator: "<="
    threshold: 1
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_justify_annotation_passes(self):
        body = """
rules:
  - name: "justified deadman"
    queries:
      - expression: |
          sum(kube_pod_status_ready{namespace="tatara"}) or vector(0)
    math_operator: "<"
    threshold: 1
    annotations:
      tatara_absence_fires: "the fabricated zero IS the intent here, see CONVENTIONS.md"
"""
        self.assertEqual(self._violations(body), [])

    def test_empty_justify_annotation_is_violation(self):
        body = """
rules:
  - name: "blank justification"
    queries:
      - expression: |
          sum(kube_pod_status_ready{namespace="tatara"}) or vector(0)
    math_operator: "<"
    threshold: 1
    annotations:
      tatara_absence_fires: "   "
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_own_exporter_up_deadman_passes(self):
        # sum(up) or vector(0) < 1 is the CORRECT deadman: it reads the alerted
        # system's own scrape series, so a fabricated zero is the real failure.
        body = """
rules:
  - name: "Operator scrape target down"
    queries:
      - expression: |
          sum(up{namespace="tatara",job="tatara-operator"}) or vector(0)
    math_operator: "<"
    threshold: 1
"""
        self.assertEqual(self._violations(body), [])

    def test_gt_threshold_with_kube_metric_passes(self):
        body = """
rules:
  - name: "Operator agent pod pool saturated with queued work"
    queries:
      - expression: |
          (sum(kube_pod_container_status_running{namespace="tatara",container="wrapper"}) or vector(0)) and (sum(operator_queue_depth{namespace="tatara"}) > 0)
    math_operator: ">"
    threshold: 5.999
"""
        self.assertEqual(self._violations(body), [])

    def test_kube_metric_without_vector_zero_passes(self):
        body = """
rules:
  - name: "Operator deployment has no available replicas"
    queries:
      - expression: |
          max(kube_deployment_status_replicas_available{namespace="tatara"}) and (max(kube_deployment_spec_replicas{namespace="tatara"}) >= 1)
    math_operator: "<"
    threshold: 1
"""
        self.assertEqual(self._violations(body), [])

    def test_or_on_vector_zero_form_is_also_matched(self):
        body = """
rules:
  - name: "or on() vector(0) variant"
    queries:
      - expression: |
          sum(kube_pod_status_ready{namespace="tatara"}) or on() vector(0)
    math_operator: "<"
    threshold: 1
"""
        self.assertEqual(len(self._violations(body)), 1)


class IdleQuantileGuard(unittest.TestCase):
    """Check 2: histogram_quantile over an empty bucket set is NaN; an idle
    service is not a slow service (CONVENTIONS.md section 1).

    These exercise lint_idle_quantile directly rather than through lint_file:
    the synthetic families below (`x`, `metricA`, ...) carry no entry in
    histogram_bounds.txt, so Check 4 legitimately fires on every one of them and
    would drown out what this class is asserting."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self, body: str):
        path = _write(self.tmp, body)
        data = yaml.safe_load(pathlib.Path(path).read_text())
        found = [lint.lint_idle_quantile(path, r) for r in data.get("rules") or []]
        return [v for v in found if v is not None]

    def test_unguarded_quantile_is_violation(self):
        body = """
rules:
  - name: "Unguarded p95"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket{namespace="tatara"}[15m])) by (le))
    math_operator: ">"
    threshold: 30
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "Unguarded p95")

    def test_count_guard_passes(self):
        body = """
rules:
  - name: "Operator turn submit p95 latency high"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket{namespace="tatara"}[15m])) by (le)) and on() (sum(rate(operator_turn_submit_duration_seconds_count{namespace="tatara"}[15m])) > 0)
    math_operator: ">"
    threshold: 30
"""
        self.assertEqual(self._violations(body), [])

    def test_justify_annotation_passes(self):
        body = """
rules:
  - name: "justified quantile"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(x_bucket[15m])) by (le))
    math_operator: ">"
    threshold: 30
    annotations:
      tatara_idle_quantile: "this histogram is never idle, it has a synthetic 1/min probe"
"""
        self.assertEqual(self._violations(body), [])

    def test_empty_justify_annotation_is_violation(self):
        body = """
rules:
  - name: "blank quantile justification"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(x_bucket[15m])) by (le))
    math_operator: ">"
    threshold: 30
    annotations:
      tatara_idle_quantile: "  "
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_non_quantile_rule_ignored(self):
        body = """
rules:
  - name: "plain rate rule"
    queries:
      - expression: |
          sum(rate(operator_reconcile_total{result="error"}[10m]))
    math_operator: ">"
    threshold: 0
"""
        self.assertEqual(self._violations(body), [])

    def test_count_guard_without_gt_zero_is_violation(self):
        # A _count reference that is not compared against zero is not a guard.
        body = """
rules:
  - name: "count present but ungated"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(x_bucket[15m])) by (le)) / clamp_min(sum(rate(x_count[15m])), 1)
    math_operator: ">"
    threshold: 30
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_guard_on_a_different_metric_family_is_violation(self):
        # The quantile is over metricA_bucket; the guard reads metricB_count. A
        # gap in metricA's own buckets still yields NaN, unnoticed.
        body = """
rules:
  - name: "cross-family guard"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(metricA_bucket[15m])) by (le)) and on() (sum(rate(metricB_count[15m])) > 0)
    math_operator: ">"
    threshold: 30
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "cross-family guard")

    def test_guard_on_the_same_metric_family_passes(self):
        # Same shape as above, but the guard is tied to the histogrammed
        # metric's own family - this is the fix's positive case.
        body = """
rules:
  - name: "same-family guard"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(metricA_bucket[15m])) by (le)) and on() (sum(rate(metricA_count[15m])) > 0)
    math_operator: ">"
    threshold: 30
"""
        self.assertEqual(self._violations(body), [])

    def test_no_extractable_bucket_family_is_violation(self):
        # histogram_quantile's own argument carries no <family>_bucket selector
        # at all (e.g. a variable/recording-rule input) - the check cannot tie
        # a guard to anything, so it is treated as unguarded rather than passed
        # silently.
        body = """
rules:
  - name: "no bucket family"
    queries:
      - expression: |
          histogram_quantile(0.95, some_recording_rule{namespace="tatara"})
    math_operator: ">"
    threshold: 30
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "no bucket family")

    def test_no_extractable_bucket_family_with_justification_passes(self):
        body = """
rules:
  - name: "no bucket family, justified"
    queries:
      - expression: |
          histogram_quantile(0.95, some_recording_rule{namespace="tatara"})
    math_operator: ">"
    threshold: 30
    annotations:
      tatara_idle_quantile: "some_recording_rule pre-aggregates buckets upstream and is never idle"
"""
        self.assertEqual(self._violations(body), [])

    def test_multiple_quantile_calls_each_need_their_own_family_guard(self):
        # Two histogram_quantile calls in one expression: the first is
        # correctly guarded on its own family, the second is not - one
        # violation, because the second family's idle NaN is still live.
        body = """
rules:
  - name: "two quantiles, one unguarded"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(metricA_bucket[15m])) by (le)) and on() (sum(rate(metricA_count[15m])) > 0) > histogram_quantile(0.95, sum(rate(metricB_bucket[15m])) by (le))
    math_operator: ">"
    threshold: 30
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "two quantiles, one unguarded")

    def test_fractional_threshold_is_not_an_idle_guard(self):
        # `> 0.2` is the alert's OWN ratio threshold, not an idle guard. The guard
        # regex must not treat the leading "0" of "0.2" as a bare `> 0` idle check
        # (fix #71-5).
        body = """
rules:
  - name: "ratio alert misread as guarded"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(x_bucket[5m])) by (le)) / sum(rate(x_count[5m])) > 0.2
    math_operator: ">"
    threshold: 0.2
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "ratio alert misread as guarded")

    def test_multiple_quantile_calls_both_guarded_passes(self):
        body = """
rules:
  - name: "two quantiles, both guarded"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(metricA_bucket[15m])) by (le)) and on() (sum(rate(metricA_count[15m])) > 0) > histogram_quantile(0.95, sum(rate(metricB_bucket[15m])) by (le)) and on() (sum(rate(metricB_count[15m])) > 0)
    math_operator: ">"
    threshold: 30
"""
        self.assertEqual(self._violations(body), [])


class SelfFiringRule(unittest.TestCase):
    """Check 3: exec_err_state Alerting makes a rule page on its own query
    failure. Grafana itself changed this default to Error in 9.2.0."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self, body: str):
        return lint.lint_file(_write(self.tmp, body))

    def test_rule_level_alerting_without_justification_is_violation(self):
        body = """
rules:
  - name: "self-firing rule"
    queries:
      - expression: |
          sum(operator_queue_depth{namespace="tatara"})
    math_operator: ">"
    threshold: 0
    exec_err_state: "Alerting"
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "self-firing rule")

    def test_rule_level_alerting_with_justification_passes(self):
        body = """
rules:
  - name: "justified self-firing rule"
    queries:
      - expression: |
          sum(operator_queue_depth{namespace="tatara"})
    math_operator: ">"
    threshold: 0
    exec_err_state: "Alerting"
    annotations:
      tatara_exec_err_justification: "an exec error here means the backend is down, which IS the condition"
"""
        self.assertEqual(self._violations(body), [])

    def test_file_level_alerting_without_justification_is_violation(self):
        body = """
default_exec_err_state: "Alerting"
rules:
  - name: "inheriting rule"
    queries:
      - expression: |
          sum(operator_queue_depth{namespace="tatara"})
    math_operator: ">"
    threshold: 0
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertIn("file", v[0].rule)

    def test_file_level_alerting_with_file_justification_passes(self):
        body = """
default_exec_err_state: "Alerting"
tatara_exec_err_justification: "Loki queries: NoData and ExecErr both also fire when the backend is degraded"
rules:
  - name: "inheriting rule"
    queries:
      - expression: |
          sum(operator_queue_depth{namespace="tatara"})
    math_operator: ">"
    threshold: 0
"""
        self.assertEqual(self._violations(body), [])

    def test_file_justification_does_not_excuse_a_rule_level_override(self):
        body = """
default_exec_err_state: "OK"
tatara_exec_err_justification: "irrelevant, the file default is OK"
rules:
  - name: "rule opts into Alerting"
    queries:
      - expression: |
          sum(operator_queue_depth{namespace="tatara"})
    math_operator: ">"
    threshold: 0
    exec_err_state: "Alerting"
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "rule opts into Alerting")

    def test_rule_overriding_an_alerting_file_default_to_error_passes(self):
        body = """
default_exec_err_state: "Alerting"
tatara_exec_err_justification: "justified at file scope"
rules:
  - name: "opts back out to Error"
    queries:
      - expression: |
          sum(operator_queue_depth{namespace="tatara"})
    math_operator: ">"
    threshold: 0
    exec_err_state: "Error"
"""
        self.assertEqual(self._violations(body), [])

    def test_no_exec_err_state_anywhere_passes(self):
        body = """
rules:
  - name: "plain rule"
    queries:
      - expression: |
          sum(operator_queue_depth{namespace="tatara"})
    math_operator: ">"
    threshold: 0
"""
        self.assertEqual(self._violations(body), [])

    def test_empty_file_justification_is_violation(self):
        body = """
default_exec_err_state: "Alerting"
tatara_exec_err_justification: "   "
rules:
  - name: "inheriting rule"
    queries:
      - expression: |
          sum(operator_queue_depth{namespace="tatara"})
    math_operator: ">"
    threshold: 0
"""
        self.assertEqual(len(self._violations(body)), 1)


class HistogramRangeGuard(unittest.TestCase):
    """Check 4: a threshold outside the histogram's representable range makes a
    rule structurally inert - it cannot fire on any input, ever, and every alert
    file's default_no_data_state: "OK" means it never goes stale either
    (tatara-observability#111)."""

    BOUNDS = {
        "operator_turn_submit_duration_seconds": (0.05, 25.6),
        "lightrag_call_duration_seconds": (0.005, 10.0),
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self, body: str):
        return lint.lint_file(_write(self.tmp, body), bounds=self.BOUNDS)

    def _rule(self, family: str, operator: str, threshold, decimal_points=None,
              annotations: str = "") -> str:
        dp = f"    decimal_points: {decimal_points}\n" if decimal_points is not None else ""
        return f"""
rules:
  - name: "p95 rule"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate({family}_bucket{{namespace="tatara"}}[15m])) by (le)) and on() (sum(rate({family}_count{{namespace="tatara"}}[15m])) > 0)
    math_operator: "{operator}"
    threshold: {threshold}
{dp}{annotations}"""

    def test_threshold_above_the_ceiling_is_violation(self):
        v = self._violations(
            self._rule("operator_turn_submit_duration_seconds", ">", 30, 1)
        )
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "p95 rule")
        self.assertIn("25.6", v[0].message)

    def test_threshold_below_the_ceiling_passes(self):
        self.assertEqual(
            self._violations(
                self._rule("operator_turn_submit_duration_seconds", ">", 6.4, 1)
            ),
            [],
        )

    def test_gt_exactly_at_the_ceiling_is_violation(self):
        # The max return value IS the top finite bound, so `> 25.6` is exactly as
        # inert as `> 30`: the predicate is strict.
        self.assertEqual(
            len(
                self._violations(
                    self._rule("operator_turn_submit_duration_seconds", ">", 25.6, 1)
                )
            ),
            1,
        )

    def test_gte_exactly_at_the_ceiling_passes(self):
        self.assertEqual(
            self._violations(
                self._rule("operator_turn_submit_duration_seconds", ">=", 25.6, 1)
            ),
            [],
        )

    def test_rounding_can_rescue_a_threshold_above_the_raw_ceiling(self):
        # decimal_points: 0 makes the reduce step round 25.6 UP to 26, so `> 25.9`
        # is reachable. A check that ignored decimal_points would false-positive a
        # legal rule (pre-mortem 4).
        self.assertEqual(
            self._violations(
                self._rule("operator_turn_submit_duration_seconds", ">", 25.9, 0)
            ),
            [],
        )

    def test_lt_below_the_quantile_floor_is_violation(self):
        # The floor is q * lowest finite bound, NOT 0. bucketQuantile's b == 0 branch
        # returns ub0 * (rank/count) with rank/count in [q, 1], so a p95 over
        # ExponentialBuckets(0.05, 2, 10) can never return below 0.95 * 0.05 = 0.0475.
        # `< 0.01` is as inert as `> 30` and must be caught.
        v = self._violations(
            self._rule("operator_turn_submit_duration_seconds", "<", 0.01, 2)
        )
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "p95 rule")

    def test_lt_above_the_quantile_floor_passes(self):
        self.assertEqual(
            self._violations(
                self._rule("operator_turn_submit_duration_seconds", "<", 0.1, 2)
            ),
            [],
        )

    def test_lt_exactly_at_the_quantile_floor_is_violation(self):
        # 0.95 * 0.05 = 0.0475, which rounds to 0.05 at decimal_points: 2. `< 0.05`
        # cannot be satisfied by a value whose minimum IS 0.05.
        self.assertEqual(
            len(
                self._violations(
                    self._rule("operator_turn_submit_duration_seconds", "<", 0.05, 2)
                )
            ),
            1,
        )

    def test_lte_exactly_at_the_quantile_floor_passes(self):
        self.assertEqual(
            self._violations(
                self._rule("operator_turn_submit_duration_seconds", "<=", 0.05, 2)
            ),
            [],
        )

    def test_lt_zero_is_violation(self):
        self.assertEqual(
            len(
                self._violations(
                    self._rule("operator_turn_submit_duration_seconds", "<", 0, 2)
                )
            ),
            1,
        )

    def test_lte_zero_is_violation_for_a_positive_domain_histogram(self):
        # histogram_quantile(0, ...) does return exactly 0, but this rule asks for
        # the p95, whose floor is 0.0475. `<= 0` is unreachable.
        self.assertEqual(
            len(
                self._violations(
                    self._rule("operator_turn_submit_duration_seconds", "<=", 0, 2)
                )
            ),
            1,
        )

    def test_a_lower_quantile_lowers_the_floor(self):
        # Same family, same threshold: the floor scales with q, so p50 reaches
        # 0.025 where p95 (floor 0.0475) does not.
        body = """
rules:
  - name: "p50 rule"
    queries:
      - expression: |
          histogram_quantile(0.5, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) and on() (sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0)
    math_operator: "<"
    threshold: 0.03
    decimal_points: 3
"""
        self.assertEqual(self._violations(body), [])
        self.assertEqual(
            len(
                self._violations(
                    body.replace("histogram_quantile(0.5,", "histogram_quantile(0.95,")
                )
            ),
            1,
        )

    SCALED = """
rules:
  - name: "p95 in milliseconds"
    queries:
      - expression: |
          1000 * histogram_quantile(0.95, sum(rate(lightrag_call_duration_seconds_bucket[10m])) by (le)) and on() (sum(rate(lightrag_call_duration_seconds_count[10m])) > 0)
    math_operator: ">"
    threshold: 5000
    decimal_points: 0
"""

    def test_scaled_quantile_is_not_range_checked_but_must_be_declared(self):
        # `1000 * histogram_quantile(...)` converts seconds to milliseconds; the
        # threshold no longer lives in the histogram's own units, so range-checking
        # it against the raw ceiling would red-build a correct rule. The shape is
        # therefore skipped - but NOT silently. A skip nobody can grep is the same
        # bypass the unknown-family hard failure exists to close, so the carve-out
        # has to be declared on the rule.
        v = self._violations(self.SCALED)
        self.assertEqual(len(v), 1)
        self.assertIn(lint.HISTOGRAM_RANGE_ANNOTATION_KEY, v[0].message)

    def test_declared_scaled_quantile_passes(self):
        body = self.SCALED + (
            "    annotations:\n"
            "      tatara_histogram_range: \"milliseconds; 5000ms is inside the 10s ceiling\"\n"
        )
        self.assertEqual(self._violations(body), [])

    def test_multi_query_quantile_rule_is_not_silently_skipped(self):
        # _joined_expressions joins the queries with a newline, so a multi-query
        # rule can never read as a bare quantile. It must still leave a signal.
        body = """
rules:
  - name: "p95 across two queries"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(lightrag_call_duration_seconds_bucket[10m])) by (le))
      - expression: |
          sum(rate(lightrag_call_duration_seconds_count[10m])) > 0
    math_operator: ">"
    threshold: 5
    annotations:
      tatara_idle_quantile: "guarded by query B"
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertIn(lint.HISTOGRAM_RANGE_ANNOTATION_KEY, v[0].message)

    def test_or_vector_in_scientific_notation_is_still_a_bare_quantile(self):
        # `or vector(1e3)` is the same shape as `or vector(1000)`; a constant the
        # regex cannot read used to drop the rule out of the check entirely.
        body = """
rules:
  - name: "p95 with a fabricated fallback"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(lightrag_call_duration_seconds_bucket[10m])) by (le)) and on() (sum(rate(lightrag_call_duration_seconds_count[10m])) > 0) or vector(1e3)
    math_operator: ">"
    threshold: 30
    decimal_points: 0
"""
        # 1000 joins the reachable set, so `> 30` over a 10s ceiling is reachable.
        self.assertEqual(self._violations(body), [])

    def test_unknown_family_is_violation(self):
        # A family with no bucket-ceiling entry is a hard FAIL, not a skip: a
        # silently-skipped unknown family is the same bypass the guard exists to
        # close (pre-mortem 5).
        v = self._violations(self._rule("some_new_duration_seconds", ">", 5, 1))
        self.assertEqual(len(v), 1)
        self.assertIn("histogram_bounds.txt", v[0].message)

    def test_justify_annotation_passes(self):
        self.assertEqual(
            self._violations(
                self._rule(
                    "operator_turn_submit_duration_seconds",
                    ">",
                    30,
                    1,
                    annotations=(
                        "    annotations:\n"
                        "      tatara_histogram_range: \"native histograms are enabled for this family upstream\"\n"
                    ),
                )
            ),
            [],
        )

    def test_empty_justify_annotation_is_violation(self):
        self.assertEqual(
            len(
                self._violations(
                    self._rule(
                        "operator_turn_submit_duration_seconds",
                        ">",
                        30,
                        1,
                        annotations=(
                            "    annotations:\n"
                            "      tatara_histogram_range: \"   \"\n"
                        ),
                    )
                )
            ),
            1,
        )

    def test_default_decimal_points_is_two(self):
        # variables.tf declares decimal_points = optional(number, 2); an omitted
        # key must not be read as 0 (which would round the ceiling up to 26).
        self.assertEqual(
            len(
                self._violations(
                    self._rule("operator_turn_submit_duration_seconds", ">", 25.9)
                )
            ),
            1,
        )

    def test_default_math_operator_is_gt(self):
        body = """
rules:
  - name: "no math_operator"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) and on() (sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0)
    threshold: 30
    decimal_points: 1
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_quantile_compared_against_another_quantile_is_not_range_checked(self):
        # Two histogram_quantile calls related by `>` with no `and` between them:
        # the value the threshold sees is a comparison between two histograms, not
        # either one's own units. Skipped rather than checked against one of the two
        # ceilings - the same reason a scaled quantile is skipped, and declared the
        # same way.
        body = """
rules:
  - name: "two quantiles"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) > histogram_quantile(0.95, sum(rate(lightrag_call_duration_seconds_bucket[15m])) by (le))
    math_operator: ">"
    threshold: 12
    decimal_points: 1
    annotations:
      tatara_idle_quantile: "comparison of two live families"
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertIn(lint.HISTOGRAM_RANGE_ANNOTATION_KEY, v[0].message)
        declared = body + '      tatara_histogram_range: "ratio of two quantiles; unitless"\n'
        self.assertEqual(self._violations(declared), [])

    def test_the_value_of_an_and_chain_is_its_left_operand(self):
        # PromQL `and` is a FILTER: `A and B` yields A's values. So a rule whose
        # expression is `<quantile> and on() (<guard>) > <other quantile> and ...`
        # thresholds the LEFT quantile (`>` binds tighter than `and`), and that one
        # is range-checkable exactly. 12 is inside [0.0475, 25.6].
        body = """
rules:
  - name: "two quantiles, and-filtered"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) and on() (sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0) > histogram_quantile(0.95, sum(rate(lightrag_call_duration_seconds_bucket[15m])) by (le)) and on() (sum(rate(lightrag_call_duration_seconds_count[15m])) > 0)
    math_operator: ">"
    threshold: 12
    decimal_points: 1
"""
        self.assertEqual(self._violations(body), [])

    def test_a_threshold_on_a_non_quantile_filtered_by_a_quantile_guard(self):
        # The value is the error rate; the quantile only filters it. There is no
        # quantile threshold to range-check, so demanding a declaration here would
        # be a red build on a correct rule.
        body = """
rules:
  - name: "errors, only while latency is bad"
    queries:
      - expression: |
          sum(rate(operator_reconcile_total{result="error"}[5m])) and on() (histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[5m])) by (le)) > 1)
    math_operator: ">"
    threshold: 5
    annotations:
      tatara_idle_quantile: "the quantile is a filter, not the value"
"""
        self.assertEqual(self._violations(body), [])

    def test_guard_written_first_thresholds_the_guard_not_the_quantile(self):
        # `(<guard>) and on() <quantile>` yields the GUARD's values, not the
        # quantile's, so there is no quantile threshold to range-check.
        body = """
rules:
  - name: "guard first"
    queries:
      - expression: |
          (sum(rate(operator_turn_submit_duration_seconds_count[5m])) > 0) and on() histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[5m])) by (le))
    math_operator: ">"
    threshold: 10
"""
        self.assertEqual(self._violations(body), [])

    def test_an_unparenthesised_idle_guard_does_not_defeat_the_check(self):
        # The guard has no wrapping parens. Matching the guard by SHAPE missed this
        # and dropped the rule out of the check; truncating at the top-level `and`
        # cannot be defeated that way. 30 is above the 25.6 ceiling.
        body = """
rules:
  - name: "unparenthesised guard"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) and on() sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0
    math_operator: ">"
    threshold: 30
    decimal_points: 1
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertIn("25.6", v[0].message)

    def test_an_and_inside_a_label_value_is_not_an_operator(self):
        body = """
rules:
  - name: "and in a label value"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket{kind=~"brainstorm and review"}[15m])) by (le))
    math_operator: ">"
    threshold: 30
    decimal_points: 1
    annotations:
      tatara_idle_quantile: "single-shot check"
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertIn("25.6", v[0].message)

    def test_non_quantile_rule_ignored(self):
        body = """
rules:
  - name: "plain rate rule"
    queries:
      - expression: |
          sum(rate(operator_reconcile_total{result="error"}[10m]))
    math_operator: ">"
    threshold: 900000
"""
        self.assertEqual(self._violations(body), [])

    def test_parenthesised_quantile_is_still_range_checked(self):
        # Wrapping the call in parens changes nothing about its units, so it must
        # not slip past the bare-quantile gate.
        body = """
rules:
  - name: "parenthesised and inert"
    queries:
      - expression: |
          (histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le))) and on() (sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0)
    math_operator: ">"
    threshold: 99999
    decimal_points: 1
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_or_vector_zero_does_not_bypass_the_ceiling(self):
        # `or vector(0)` widens the reachable set downward by adding 0; it cannot
        # lift the ceiling, so an above-ceiling `>` threshold is still inert.
        body = """
rules:
  - name: "fabricated zero, still inert"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) and on() (sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0) or vector(0)
    math_operator: ">"
    threshold: 99999
    decimal_points: 1
"""
        self.assertEqual(len(self._violations(body)), 1)

    def test_or_vector_zero_widens_the_floor(self):
        # ... and it DOES make `< 0.01` reachable, because 0 is now in the set.
        body = """
rules:
  - name: "fabricated zero reaches below the floor"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) and on() (sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0) or vector(0)
    math_operator: "<"
    threshold: 0.01
    decimal_points: 2
"""
        self.assertEqual(self._violations(body), [])

    def test_a_quantile_inside_the_idle_guard_is_not_range_checked(self):
        # The rule's own value comes from the FIRST quantile (ceiling 25.6); the
        # second lives inside the `and on() (...)` guard and its 2.56 ceiling has
        # nothing to do with this threshold. Range-checking it would red-build a
        # correct rule.
        body = """
rules:
  - name: "quantile in the guard"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) and on() (histogram_quantile(0.95, sum(rate(operator_webhook_duration_seconds_bucket[5m])) by (le)) > 0)
    math_operator: ">"
    threshold: 10
    decimal_points: 1
    annotations:
      tatara_idle_quantile: "guarded by the companion quantile"
"""
        self.assertEqual(self._violations(body), [])

    def test_unknown_family_message_names_the_committed_file_format(self):
        v = self._violations(self._rule("some_new_duration_seconds", ">", 5, 1))
        self.assertEqual(len(v), 1)
        # The remediation the message prescribes must actually parse.
        self.assertIn("<lowest finite bound> <top finite bound>", v[0].message)

    def test_unidentifiable_bucket_family_is_violation(self):
        # No <family>_bucket selector at all: no ceiling can be looked up, so the
        # range cannot be verified. Fails closed (Check 2 also flags this shape).
        body = """
rules:
  - name: "recording rule input"
    queries:
      - expression: |
          histogram_quantile(0.95, some_recording_rule{namespace="tatara"})
    math_operator: ">"
    threshold: 30
    annotations:
      tatara_idle_quantile: "pre-aggregated upstream, never idle"
"""
        v = self._violations(body)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].rule, "recording rule input")


class HistogramBoundsFile(unittest.TestCase):
    """The committed scripts/histogram_bounds.txt must parse, and must carry an
    entry for every family the live alert files run a quantile over."""

    def test_committed_bounds_file_parses(self):
        bounds = lint.load_histogram_bounds()
        self.assertIn("operator_turn_submit_duration_seconds", bounds)
        self.assertEqual(bounds["operator_turn_submit_duration_seconds"], (0.05, 25.6))
        self.assertEqual(bounds["lightrag_call_duration_seconds"], (0.005, 10.0))

    def test_every_entry_has_a_lower_bound_below_its_upper(self):
        for family, (low, high) in lint.load_histogram_bounds().items():
            self.assertLess(low, high, family)

    def test_a_three_field_entry_the_message_prescribes_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "b.txt"
            p.write_text("# --- memory: x ---\nsome_new_duration_seconds 0.005 10\n")
            self.assertEqual(
                lint.load_histogram_bounds(p),
                {"some_new_duration_seconds": (0.005, 10.0)},
            )

    def test_inverted_bounds_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "b.txt"
            p.write_text("foo 10 1\n")
            with self.assertRaises(ValueError):
                lint.load_histogram_bounds(p)

    def test_non_finite_bounds_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "b.txt"
            p.write_text("foo nan inf\n")
            with self.assertRaises(ValueError):
                lint.load_histogram_bounds(p)


class RoundingModel(unittest.TestCase):
    def test_half_away_from_zero_not_bankers(self):
        # Python's round(0.5) is 0 and round(2.5) is 2; Grafana's math round() is
        # Go math.Round, which gives 1 and 3.
        self.assertEqual(lint._rounded(0.5, 0), 1.0)
        self.assertEqual(lint._rounded(2.5, 0), 3.0)
        self.assertEqual(lint._rounded(25.6, 0), 26.0)
        self.assertEqual(lint._rounded(25.6, 1), 25.6)

    def test_negative_decimal_points(self):
        self.assertEqual(lint._rounded(25.6, -1), 30.0)

    def test_extreme_decimal_points_does_not_overflow(self):
        # decimal_points is optional(number, 2) in variables.tf with no upper
        # bound; 10**309 as a Python int overflows float conversion.
        self.assertEqual(lint._rounded(25.6, 400), 25.6)


class DashboardThresholdRange(unittest.TestCase):
    """Check 5: a dashboard panel's red threshold step over a quantile the panel
    cannot reach. Same silent-green class as Check 4 one surface over - the band
    never colours, so the panel reads healthy because it cannot read anything else
    (tatara-observability#111 review round 1)."""

    BOUNDS = {
        "operator_turn_submit_duration_seconds": (0.05, 25.6),
        "lightrag_call_duration_seconds": (0.005, 10.0),
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self, dashboard: dict):
        p = self.tmp / "d.json"
        p.write_text(json.dumps(dashboard))
        return lint.lint_file(str(p), bounds=self.BOUNDS)

    def _panel(self, exprs, steps, title="p95 panel"):
        return {
            "title": title,
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "targets": [{"expr": e, "refId": chr(65 + i)} for i, e in enumerate(exprs)],
            "fieldConfig": {
                "defaults": {
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [{"color": "green", "value": None}]
                        + [{"color": "red", "value": s} for s in steps],
                    }
                }
            },
        }

    QUANTILE = (
        "histogram_quantile(0.95, sum(rate({f}_bucket[15m])) by (le, kind))"
    )

    def test_step_above_the_ceiling_is_violation(self):
        v = self._violations(
            {
                "panels": [
                    self._panel(
                        [self.QUANTILE.format(f="operator_turn_submit_duration_seconds")],
                        [30],
                    )
                ]
            }
        )
        self.assertEqual(len(v), 1)
        self.assertIn("25.6", v[0].message)
        self.assertIn("p95 panel", v[0].rule)

    def test_step_below_the_ceiling_passes(self):
        self.assertEqual(
            self._violations(
                {
                    "panels": [
                        self._panel(
                            [self.QUANTILE.format(f="operator_turn_submit_duration_seconds")],
                            [6.4],
                        )
                    ]
                }
            ),
            [],
        )

    def test_step_exactly_at_the_ceiling_passes(self):
        # A Grafana threshold step colours at value >= step, so a step sitting
        # exactly on the ceiling is attained, not inert.
        self.assertEqual(
            self._violations(
                {
                    "panels": [
                        self._panel(
                            [self.QUANTILE.format(f="operator_turn_submit_duration_seconds")],
                            [25.6],
                        )
                    ]
                }
            ),
            [],
        )

    def test_panel_with_no_finite_step_is_skipped(self):
        panel = self._panel(
            [self.QUANTILE.format(f="lightrag_call_duration_seconds")], []
        )
        self.assertEqual(self._violations({"panels": [panel]}), [])

    def test_non_quantile_panel_is_skipped(self):
        panel = self._panel(["sum(rate(lightrag_calls_total[5m]))"], [30])
        self.assertEqual(self._violations({"panels": [panel]}), [])

    def test_row_collapsed_panels_are_walked(self):
        inner = self._panel(
            [self.QUANTILE.format(f="lightrag_call_duration_seconds")], [30]
        )
        row = {"type": "row", "title": "row", "panels": [inner]}
        v = self._violations({"panels": [row]})
        self.assertEqual(len(v), 1)

    def test_the_highest_target_ceiling_wins(self):
        # A panel plotting two families is unreachable only above BOTH ceilings.
        panel = self._panel(
            [
                self.QUANTILE.format(f="lightrag_call_duration_seconds"),
                self.QUANTILE.format(f="operator_turn_submit_duration_seconds"),
            ],
            [20],
        )
        self.assertEqual(self._violations({"panels": [panel]}), [])
        panel = self._panel(
            [
                self.QUANTILE.format(f="lightrag_call_duration_seconds"),
                self.QUANTILE.format(f="operator_turn_submit_duration_seconds"),
            ],
            [30],
        )
        self.assertEqual(len(self._violations({"panels": [panel]})), 1)

    def test_unknown_family_is_violation(self):
        panel = self._panel([self.QUANTILE.format(f="some_new_duration_seconds")], [30])
        v = self._violations({"panels": [panel]})
        self.assertEqual(len(v), 1)
        self.assertIn("histogram_bounds.txt", v[0].message)

    def test_scaled_quantile_panel_is_skipped(self):
        # Milliseconds on the axis: the step is not in the histogram's units. A
        # panel carries no annotations, so unlike Check 4 there is nothing to
        # declare - the carve-out is documented in CONVENTIONS.md 6.4 instead.
        panel = self._panel(
            ["1000 * " + self.QUANTILE.format(f="lightrag_call_duration_seconds")],
            [30000],
        )
        self.assertEqual(self._violations({"panels": [panel]}), [])

    def test_mixed_quantile_and_non_quantile_targets_are_skipped(self):
        panel = self._panel(
            [
                self.QUANTILE.format(f="lightrag_call_duration_seconds"),
                "sum(rate(lightrag_calls_total[5m]))",
            ],
            [30],
        )
        self.assertEqual(self._violations({"panels": [panel]}), [])

    def test_an_out_of_model_target_does_not_switch_the_check_off_by_order(self):
        # The verdict must not depend on target order. A panel mixing a bare
        # quantile with a scaled one cannot be range-checked (a step may be about
        # the other series), but it must skip identically either way round.
        bare = self.QUANTILE.format(f="operator_turn_submit_duration_seconds")
        scaled = "1000 * " + bare
        for exprs in ([bare, scaled], [scaled, bare]):
            self.assertEqual(
                self._violations({"panels": [self._panel(exprs, [30])]}), [], exprs
            )

    def test_unknown_family_is_reported_whatever_the_target_order(self):
        known = self.QUANTILE.format(f="operator_turn_submit_duration_seconds")
        unknown = self.QUANTILE.format(f="some_new_duration_seconds")
        for exprs in ([known, unknown], [unknown, known]):
            v = self._violations({"panels": [self._panel(exprs, [30])]})
            self.assertEqual(len(v), 1, exprs)
            self.assertIn("histogram_bounds.txt", v[0].message)

    def test_an_unreachable_step_is_found_behind_an_earlier_clean_target(self):
        # An early in-range target must not end the scan.
        panel = self._panel(
            [
                self.QUANTILE.format(f="lightrag_call_duration_seconds"),
                self.QUANTILE.format(f="lightrag_call_duration_seconds"),
            ],
            [30],
        )
        self.assertEqual(len(self._violations({"panels": [panel]})), 1)

    def test_percentage_mode_steps_are_not_absolute_values(self):
        # In percentage mode a step of 80 means 80% of min..max, not 80 seconds.
        panel = self._panel(
            [self.QUANTILE.format(f="operator_turn_submit_duration_seconds")], [80]
        )
        panel["fieldConfig"]["defaults"]["thresholds"]["mode"] = "percentage"
        self.assertEqual(self._violations({"panels": [panel]}), [])

    def test_a_numeric_string_step_value_is_still_a_step(self):
        panel = self._panel(
            [self.QUANTILE.format(f="operator_turn_submit_duration_seconds")], []
        )
        panel["fieldConfig"]["defaults"]["thresholds"]["steps"].append(
            {"color": "red", "value": "30"}
        )
        self.assertEqual(len(self._violations({"panels": [panel]})), 1)

    def test_a_per_series_threshold_override_is_range_checked(self):
        # fieldConfig.overrides[].properties[].id == "thresholds" is a live idiom in
        # dashboards/task-delivery.json; a red step hidden there must not bypass.
        panel = self._panel(
            [self.QUANTILE.format(f="operator_turn_submit_duration_seconds")], []
        )
        panel["fieldConfig"]["overrides"] = [
            {
                "matcher": {"id": "byName", "options": "p95"},
                "properties": [
                    {
                        "id": "thresholds",
                        "value": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "green", "value": None},
                                {"color": "red", "value": 30},
                            ],
                        },
                    }
                ],
            }
        ]
        v = self._violations({"panels": [panel]})
        self.assertEqual(len(v), 1)
        self.assertIn("25.6", v[0].message)

    def test_thresholds_style_off_is_not_rendered_and_not_failed(self):
        # thresholdsStyle.mode "off" means the band is never drawn. 6 of the 8
        # quantile panels in dashboards/ carry it; failing on leftover steps there
        # would be a red build on a panel with no user-visible defect.
        panel = self._panel(
            [self.QUANTILE.format(f="operator_turn_submit_duration_seconds")], [30]
        )
        panel["fieldConfig"]["defaults"]["custom"] = {"thresholdsStyle": {"mode": "off"}}
        self.assertEqual(self._violations({"panels": [panel]}), [])

    def test_a_legacy_string_datasource_is_still_prometheus(self):
        panel = self._panel(
            [self.QUANTILE.format(f="operator_turn_submit_duration_seconds")], [30]
        )
        panel["datasource"] = "prometheus"
        self.assertEqual(len(self._violations({"panels": [panel]})), 1)

    def test_a_loki_target_is_not_read_as_promql(self):
        panel = self._panel(
            [self.QUANTILE.format(f="lightrag_call_duration_seconds")], [30]
        )
        panel["datasource"] = {"type": "loki", "uid": "loki"}
        self.assertEqual(self._violations({"panels": [panel]}), [])


class RealFilesPass(unittest.TestCase):
    def test_all_committed_alert_files_pass(self):
        paths = sorted(str(p) for p in ALERTS_DIR.glob("*.yaml"))
        self.assertTrue(paths, "expected alerts/*.yaml to exist")
        violations = lint.lint_paths(paths)
        self.assertEqual(
            violations, [], "committed alert rules must satisfy the convention:\n"
            + "\n".join(str(v) for v in violations),
        )

    def test_all_committed_dashboards_pass(self):
        paths = sorted(str(p) for p in DASHBOARDS_DIR.glob("*.json"))
        self.assertTrue(paths, "expected dashboards/*.json to exist")
        violations = lint.lint_paths(paths)
        self.assertEqual(
            violations, [], "committed dashboard panels must satisfy the convention:\n"
            + "\n".join(str(v) for v in violations),
        )

    def test_default_paths_cover_both_surfaces(self):
        paths = lint._default_paths()
        self.assertTrue(any(p.endswith(".yaml") for p in paths))
        self.assertTrue(any(p.endswith(".json") for p in paths))


if __name__ == "__main__":
    unittest.main()
