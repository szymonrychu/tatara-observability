#!/usr/bin/env python3
"""Tests for lint_alert_rules. Run: python3 -m unittest scripts.test_lint_alert_rules
or, from the scripts/ dir: python3 -m unittest test_lint_alert_rules."""

import pathlib
import tempfile
import unittest

import yaml

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

    def test_scaled_quantile_is_not_range_checked(self):
        # `1000 * histogram_quantile(...)` converts seconds to milliseconds; the
        # threshold no longer lives in the histogram's own units, so range-checking
        # it against the raw ceiling would red-build a correct rule.
        body = """
rules:
  - name: "p95 in milliseconds"
    queries:
      - expression: |
          1000 * histogram_quantile(0.95, sum(rate(lightrag_call_duration_seconds_bucket[10m])) by (le)) and on() (sum(rate(lightrag_call_duration_seconds_count[10m])) > 0)
    math_operator: ">"
    threshold: 5000
    decimal_points: 0
"""
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
        # Two histogram_quantile calls related by `>`: the value the threshold sees
        # is the result of a comparison between two histograms, not either one's
        # own units. Skipped rather than checked against one of the two ceilings -
        # the same reason a scaled quantile is skipped. Check 2 still covers both
        # calls' idle guards.
        body = """
rules:
  - name: "two quantiles"
    queries:
      - expression: |
          histogram_quantile(0.95, sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)) and on() (sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0) > histogram_quantile(0.95, sum(rate(lightrag_call_duration_seconds_bucket[15m])) by (le)) and on() (sum(rate(lightrag_call_duration_seconds_count[15m])) > 0)
    math_operator: ">"
    threshold: 12
    decimal_points: 1
"""
        self.assertEqual(self._violations(body), [])

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
