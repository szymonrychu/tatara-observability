#!/usr/bin/env python3
"""Tests for check_chart_alert_parity. Run:
python3 -m unittest scripts.test_check_chart_alert_parity or, from the scripts/ dir:
python3 -m unittest test_check_chart_alert_parity.

No network calls: the cross-repo half is exercised by feeding chart_alerts a template
string, never a real clone."""

import pathlib
import tempfile
import unittest

from check_chart_alert_parity import (
    Waiver,
    chart_alerts,
    load_waivers,
    ported_metrics,
    reconcile,
)

# A miniature of tatara-operator's prometheusrule.yaml: a plain expr, a Go-templated
# threshold, a block scalar, and a templated range selector.
CHART = """{{- if .Values.prometheusRule.enabled }}
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
spec:
  groups:
    - name: tatara-operator
      rules:
        - alert: TataraPlain
          expr: operator_reconcile_total{result="error"} > 0
          labels:
            severity: {{ .Values.prometheusRule.severityLabel | quote }}
          annotations:
            summary: "plain"
        - alert: TataraTemplatedThreshold
          expr: operator_tasks_inflight >= {{ .Values.prometheusRule.tasksInflightThreshold }}
          for: 2h
          annotations:
            summary: "templated"
        - alert: TataraBlockScalar
          expr: |
            histogram_quantile(0.95,
              sum(rate(operator_turn_submit_duration_seconds_bucket[15m])) by (le)
            ) > {{ .Values.prometheusRule.turnSubmitP95LatencyThreshold }}
            and
            sum(rate(operator_turn_submit_duration_seconds_count[15m])) > 0
          for: 15m
          annotations:
            summary: "block"
        - alert: TataraTemplatedRange
          expr: increase(operator_sweep_skipped_total{reason!="x"}[{{ .Values.prometheusRule.sweepSkipWindow }}]) >= {{ .Values.prometheusRule.sweepSkipPassThreshold }}
          annotations:
            summary: "range"
{{- end }}
"""


class ChartAlertsTest(unittest.TestCase):
    def test_finds_every_alert(self):
        self.assertEqual(
            set(chart_alerts(CHART)),
            {
                "TataraPlain",
                "TataraTemplatedThreshold",
                "TataraBlockScalar",
                "TataraTemplatedRange",
            },
        )

    def test_plain_expr_yields_its_metric(self):
        self.assertEqual(chart_alerts(CHART)["TataraPlain"], {"operator_reconcile_total"})

    def test_a_go_template_threshold_is_stripped_and_is_not_a_metric(self):
        # `{{ .Values... }}` left in place would fall through as bare identifiers and
        # invent metrics named `Values` or `prometheusRule`.
        self.assertEqual(
            chart_alerts(CHART)["TataraTemplatedThreshold"], {"operator_tasks_inflight"}
        )

    def test_a_block_scalar_expr_is_read_to_its_end(self):
        # Stopping at the first line would miss the `_count` term on the second half of
        # the expression - and _count/_bucket both normalise to the base name, the way
        # check_metric_provenance does it.
        self.assertEqual(
            chart_alerts(CHART)["TataraBlockScalar"],
            {"operator_turn_submit_duration_seconds"},
        )

    def test_a_templated_range_selector_does_not_break_extraction(self):
        self.assertEqual(
            chart_alerts(CHART)["TataraTemplatedRange"], {"operator_sweep_skipped_total"}
        )


class PortedMetricsTest(unittest.TestCase):
    def _alert_file(self, tmp: str, body: str) -> str:
        path = pathlib.Path(tmp) / "tatara-x.yaml"
        path.write_text(body)
        return str(path)

    def test_collects_metrics_from_every_rule_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._alert_file(
                tmp,
                "rules:\n"
                '  - name: "one"\n'
                "    queries:\n"
                "      - expression: |\n"
                "          sum(increase(operator_alpha_total[1h])) or vector(0)\n"
                "    threshold: 0\n"
                '  - name: "two"\n'
                "    queries:\n"
                "      - expression: |\n"
                "          max(operator_beta_seconds)\n"
                "    threshold: 0\n",
            )
            self.assertEqual(
                ported_metrics([path]), {"operator_alpha_total", "operator_beta_seconds"}
            )

    def test_a_loki_query_contributes_no_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._alert_file(
                tmp,
                "rules:\n"
                '  - name: "log"\n'
                "    queries:\n"
                "      - expression: |\n"
                '          sum(count_over_time({app="x"} | json | level="ERROR" [5m]))\n'
                "        query_type: loki\n"
                "    threshold: 0\n",
            )
            self.assertEqual(ported_metrics([path]), set())


class ReconcileTest(unittest.TestCase):
    CHART_MAP = {
        "TataraAlpha": {"operator_alpha_total"},
        "TataraBeta": {"operator_beta_total"},
    }

    def test_a_metric_no_rule_reads_is_a_violation(self):
        v = reconcile(self.CHART_MAP, {"operator_alpha_total"}, {})
        self.assertEqual([(x.alert, x.metric) for x in v], [("TataraBeta", "operator_beta_total")])

    def test_everything_ported_is_clean(self):
        self.assertEqual(
            reconcile(self.CHART_MAP, {"operator_alpha_total", "operator_beta_total"}, {}),
            [],
        )

    def test_a_keyed_waiver_suppresses_exactly_its_own_pair(self):
        waivers = {("TataraBeta", "operator_beta_total"): Waiver("replaced by X")}
        self.assertEqual(reconcile(self.CHART_MAP, {"operator_alpha_total"}, waivers), [])

    def test_a_waiver_keyed_on_a_different_alert_does_not_suppress(self):
        # The key is (alert, metric), not metric alone: two chart alerts may read the
        # same metric and only one of them be legitimately replaced.
        waivers = {("TataraAlpha", "operator_beta_total"): Waiver("wrong alert")}
        v = reconcile(self.CHART_MAP, {"operator_alpha_total"}, waivers)
        self.assertEqual([(x.alert, x.metric) for x in v], [("TataraBeta", "operator_beta_total")])


class LoadWaiversTest(unittest.TestCase):
    def _write(self, tmp: str, body: str) -> pathlib.Path:
        path = pathlib.Path(tmp) / "waivers.txt"
        path.write_text(body)
        return path

    def test_parses_alert_metric_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "# a comment\n"
                "\n"
                "TataraBeta operator_beta_total # replaced by the Foo rule\n",
            )
            waivers = load_waivers(path)
            self.assertEqual(list(waivers), [("TataraBeta", "operator_beta_total")])
            self.assertEqual(
                waivers[("TataraBeta", "operator_beta_total")].reason,
                "replaced by the Foo rule",
            )

    def test_a_waiver_with_no_reason_is_rejected(self):
        # A bare waiver is how a coverage gap gets silently blessed. The reason is the
        # only artifact that makes the next reader able to disagree with it.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "TataraBeta operator_beta_total\n")
            with self.assertRaises(ValueError):
                load_waivers(path)

    def test_a_malformed_line_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "OnlyOneField # why\n")
            with self.assertRaises(ValueError):
                load_waivers(path)


class LiveTreeTest(unittest.TestCase):
    """The shipped waiver file must parse, and must not carry a waiver for a metric that
    alerts/ actually reads - a waiver that has become unnecessary is stale documentation
    claiming a gap that is closed."""

    def test_shipped_waivers_parse(self):
        root = pathlib.Path(__file__).resolve().parent
        waivers = load_waivers(root / "chart_alert_waivers.txt")
        self.assertTrue(waivers)
        for (alert, metric), waiver in waivers.items():
            self.assertTrue(waiver.reason, f"{alert}/{metric} has an empty reason")

    def test_no_shipped_waiver_covers_a_metric_alerts_already_read(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        paths = sorted(str(p) for p in (root / "alerts").glob("*.yaml"))
        read = ported_metrics(paths)
        redundant = [
            f"{alert}/{metric}"
            for (alert, metric) in load_waivers(root / "scripts" / "chart_alert_waivers.txt")
            if metric in read
        ]
        self.assertEqual(redundant, [])


if __name__ == "__main__":
    unittest.main()
