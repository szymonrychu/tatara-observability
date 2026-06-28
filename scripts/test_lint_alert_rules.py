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
