#!/usr/bin/env python3
"""Tests for lint_alert_rules. Run: python3 -m unittest scripts.test_lint_alert_rules
or, from the scripts/ dir: python3 -m unittest test_lint_alert_rules."""

import pathlib
import tempfile
import unittest

import lint_alert_rules as lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ALERTS_DIR = REPO_ROOT / "alerts"


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
    service is not a slow service (CONVENTIONS.md section 1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self, body: str):
        return lint.lint_file(_write(self.tmp, body))

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


class RealAlertFilesPass(unittest.TestCase):
    def test_all_committed_alert_files_pass(self):
        paths = sorted(str(p) for p in ALERTS_DIR.glob("*.yaml"))
        self.assertTrue(paths, "expected alerts/*.yaml to exist")
        violations = lint.lint_paths(paths)
        self.assertEqual(
            violations, [], "committed alert rules must satisfy the convention:\n"
            + "\n".join(str(v) for v in violations),
        )


if __name__ == "__main__":
    unittest.main()
