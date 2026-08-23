#!/usr/bin/env python3
"""Tests for check_alert_plane_parity. Run:
python3 -m unittest scripts.test_check_alert_plane_parity or, from the scripts/ dir:
python3 -m unittest test_check_alert_plane_parity.

No network calls: the cross-repo half is exercised by feeding chart_alerts a template
string and go_alerts a Go source string, never a real clone."""

import pathlib
import tempfile
import unittest

from check_alert_plane_parity import (
    UnresolvedExpr,
    Waiver,
    chart_alerts,
    go_alerts,
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

# A miniature of tatara-operator/internal/memory/monitoring.go, carrying every expr
# SHAPE the real file uses. Each one is a way a metric name can hide from a regex that
# only reads intstr.FromString("..."):
#   - a bare backtick literal                       (GoPlain)
#   - a literal concatenated with a package const   (GoConstConcat)
#   - fmt.Sprintf whose FORMAT STRING carries the metric, args are selectors (GoSprintf)
#   - a file-local variable holding a whole PromQL fragment, interpolated (GoLocalVar)
#   - an indexed verb used twice                    (GoIndexedVerb)
GO = '''package memory

const (
	goRatio     = "0.05"
	goThreshold = "300"
)

func memoryAlertRules(p *Project, cluster, namespace string, backupEnabled bool) []Rule {
	podSelector := fmt.Sprintf(`namespace=%q, pod=~%q, container="postgres"`, namespace, cluster+"-.*")
	onPrimary := fmt.Sprintf(`and on(pod) (cnpg_pg_replication_in_recovery{%s} == 0)`, podSelector)
	instances := PgInstances(p)

	rules := []monitoringv1.Rule{
		{
			Alert:  "GoPlain",
			Expr:   intstr.FromString(`up{job=~".*tatara-memory.*"} == 0`),
			For:    dur("5m"),
		},
		{
			Alert: "GoConstConcat",
			Expr: intstr.FromString(
				`sum(rate(go_alpha_total[5m])) > ` + goRatio,
			),
			For: dur("10m"),
		},
		{
			Alert: "GoSprintf",
			Expr: intstr.FromString(fmt.Sprintf(
				`increase(go_beta_restarts_total{%s}[15m]) > 2`,
				podSelector,
			)),
			For: dur("5m"),
		},
		{
			Alert: "GoLocalVar",
			Expr: intstr.FromString(fmt.Sprintf(
				`max by (slot_name) (go_gamma_slots_active{%s} %s) == 0`,
				podSelector, onPrimary,
			)),
			For: dur("30m"),
		},
		{
			Alert: "GoIndexedVerb",
			Expr: intstr.FromString(fmt.Sprintf(
				`go_delta_available_bytes{%[1]s} / go_delta_capacity_bytes{%[1]s} < 0.15`,
				podSelector,
			)),
			For: dur("5m"),
		},
		{
			Alert: "GoNumericThreshold",
			Expr: intstr.FromString(fmt.Sprintf(
				`(count(up{%s} == 1) or vector(0)) < %d`,
				podSelector, instances,
			)),
			For: dur("10m"),
		},
		{
			Alert: "GoConstVerb",
			Expr: intstr.FromString(fmt.Sprintf(
				`max by (application_name) (go_epsilon_replay_lag_seconds{%s}) > %s`,
				podSelector, goThreshold,
			)),
			For: dur("15m"),
		},
	}
	return rules
}
'''


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


class GoAlertsTest(unittest.TestCase):
    """The Go reader. Every test here is a shape the REAL monitoring.go uses; a reader
    that handles only the first one reports 7 of 8 postgres metrics and prints OK,
    which is the defect this whole check exists to catch, one level down."""

    def test_finds_every_alert(self):
        self.assertEqual(
            set(go_alerts(GO)),
            {
                "GoPlain",
                "GoConstConcat",
                "GoSprintf",
                "GoLocalVar",
                "GoIndexedVerb",
                "GoNumericThreshold",
                "GoConstVerb",
            },
        )

    def test_a_bare_literal_expr_yields_its_metric(self):
        self.assertEqual(go_alerts(GO)["GoPlain"], {"up"})

    def test_a_package_const_concatenation_is_resolved(self):
        # `... + goRatio` left unresolved would leave a bare identifier in threshold
        # position; resolved it is "0.05" and contributes no metric.
        self.assertEqual(go_alerts(GO)["GoConstConcat"], {"go_alpha_total"})

    def test_a_sprintf_format_string_carries_its_metric(self):
        # 9 of the 15 real rules are this shape. A regex over intstr.FromString("...")
        # sees the fmt.Sprintf call, not the format string, and reads nothing.
        self.assertEqual(go_alerts(GO)["GoSprintf"], {"go_beta_restarts_total"})

    def test_a_local_variable_holding_a_promql_fragment_is_resolved(self):
        # onPrimary is a LOCAL holding `... cnpg_pg_replication_in_recovery{...} ...`,
        # interpolated into three real rules. Leaving it unresolved loses that metric
        # entirely and it is read by no rule in alerts/.
        self.assertEqual(
            go_alerts(GO)["GoLocalVar"],
            {"go_gamma_slots_active", "cnpg_pg_replication_in_recovery"},
        )

    def test_an_indexed_verb_substitutes_at_every_site(self):
        self.assertEqual(
            go_alerts(GO)["GoIndexedVerb"],
            {"go_delta_available_bytes", "go_delta_capacity_bytes"},
        )

    def test_an_unresolvable_numeric_verb_is_not_a_metric_and_is_not_an_error(self):
        # `%d` is type-checked by the compiler to take an integer, so it can never
        # introduce a metric name. Sentinel-checking it would hard-fail on today's tree.
        self.assertEqual(go_alerts(GO)["GoNumericThreshold"], {"up"})

    def test_a_const_string_verb_is_resolved(self):
        self.assertEqual(
            go_alerts(GO)["GoConstVerb"], {"go_epsilon_replay_lag_seconds"}
        )

    def test_an_unresolvable_string_verb_in_metric_position_is_a_hard_error(self):
        # The fail-closed half. A `%s` the reader cannot resolve MIGHT be a metric
        # name; if it lands in metric-name position, reporting the rest as clean is
        # exactly the "a competent audit of the wrong plane" failure.
        src = GO.replace(
            "`up{job=~\".*tatara-memory.*\"} == 0`",
            "fmt.Sprintf(`%s{job=\"x\"} == 0`, mysteryMetric)",
        )
        with self.assertRaises(UnresolvedExpr) as ctx:
            go_alerts(src)
        self.assertIn("GoPlain", str(ctx.exception))

    def test_an_unresolvable_string_verb_inside_a_selector_body_is_not_an_error(self):
        # podSelector's own inner %q args (namespace, cluster) are function params the
        # reader cannot resolve. They land inside `{...}`, which metric_names strips,
        # so they cannot hide a metric name and must not fail the build.
        self.assertEqual(go_alerts(GO)["GoSprintf"], {"go_beta_restarts_total"})

    def test_a_file_with_no_alerts_yields_nothing(self):
        # main() turns this into a hard error per source; the reader itself just
        # reports what it found.
        self.assertEqual(go_alerts("package memory\n"), {})


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
    PLANE = "operator-chart"
    ALERTS = {
        "TataraAlpha": {"operator_alpha_total"},
        "TataraBeta": {"operator_beta_total"},
    }

    def test_a_metric_no_rule_reads_is_a_violation(self):
        v = reconcile(self.PLANE, self.ALERTS, {"operator_alpha_total"}, {})
        self.assertEqual(
            [(x.plane, x.alert, x.metric) for x in v],
            [("operator-chart", "TataraBeta", "operator_beta_total")],
        )

    def test_everything_ported_is_clean(self):
        self.assertEqual(
            reconcile(
                self.PLANE,
                self.ALERTS,
                {"operator_alpha_total", "operator_beta_total"},
                {},
            ),
            [],
        )

    def test_a_keyed_waiver_suppresses_exactly_its_own_pair(self):
        waivers = {
            ("operator-chart", "TataraBeta", "operator_beta_total"): Waiver(
                "SUPERSEDED", "replaced by X", None
            )
        }
        self.assertEqual(
            reconcile(self.PLANE, self.ALERTS, {"operator_alpha_total"}, waivers), []
        )

    def test_a_waiver_keyed_on_a_different_alert_does_not_suppress(self):
        # The key is (plane, alert, metric), not metric alone: two alerts may read the
        # same metric and only one of them be legitimately replaced.
        waivers = {
            ("operator-chart", "TataraAlpha", "operator_beta_total"): Waiver(
                "SUPERSEDED", "wrong alert", None
            )
        }
        v = reconcile(self.PLANE, self.ALERTS, {"operator_alpha_total"}, waivers)
        self.assertEqual(
            [(x.plane, x.alert, x.metric) for x in v],
            [("operator-chart", "TataraBeta", "operator_beta_total")],
        )

    def test_a_waiver_on_a_different_plane_does_not_suppress(self):
        # MemoryDown exists on BOTH the memory chart and the operator Go plane. An
        # unqualified key would let one plane's waiver silently blanket the other,
        # which is the failure this file exists to prevent.
        waivers = {
            ("operator-go", "TataraBeta", "operator_beta_total"): Waiver(
                "DORMANT-PRODUCER", "off", "spec.memory.enabled: true"
            )
        }
        v = reconcile(self.PLANE, self.ALERTS, {"operator_alpha_total"}, waivers)
        self.assertEqual(
            [(x.plane, x.alert, x.metric) for x in v],
            [("operator-chart", "TataraBeta", "operator_beta_total")],
        )


class LoadWaiversTest(unittest.TestCase):
    def _write(self, tmp: str, body: str) -> pathlib.Path:
        path = pathlib.Path(tmp) / "waivers.txt"
        path.write_text(body)
        return path

    def test_parses_plane_alert_metric_class_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "# a comment\n"
                "\n"
                "operator-chart:TataraBeta operator_beta_total "
                "# SUPERSEDED: replaced by the Foo rule\n",
            )
            waivers = load_waivers(path)
            key = ("operator-chart", "TataraBeta", "operator_beta_total")
            self.assertEqual(list(waivers), [key])
            self.assertEqual(waivers[key].klass, "SUPERSEDED")
            self.assertEqual(waivers[key].reason, "replaced by the Foo rule")

    def test_a_waiver_with_no_reason_is_rejected(self):
        # A bare waiver is how a coverage gap gets silently blessed. The reason is the
        # only artifact that makes the next reader able to disagree with it.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "operator-chart:TataraBeta operator_beta_total\n")
            with self.assertRaises(ValueError):
                load_waivers(path)

    def test_a_waiver_with_no_class_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp, "operator-chart:TataraBeta operator_beta_total # replaced by Foo\n"
            )
            with self.assertRaises(ValueError):
                load_waivers(path)

    def test_an_unqualified_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp, "TataraBeta operator_beta_total # SUPERSEDED: replaced by Foo\n"
            )
            with self.assertRaises(ValueError):
                load_waivers(path)

    def test_an_unknown_plane_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp, "made-up:TataraBeta operator_beta_total # SUPERSEDED: why\n"
            )
            with self.assertRaises(ValueError):
                load_waivers(path)

    def test_dormant_producer_requires_a_re_arm_clause(self):
        # A dormancy waiver with no stated re-arm precondition is indistinguishable
        # from "we decided not to", which the header forbids.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "operator-go:MemoryPostgresWALArchiveBacklog cnpg_collector_pg_wal_archive_status "
                "# DORMANT-PRODUCER: never generated on this cluster\n",
            )
            with self.assertRaises(ValueError):
                load_waivers(path)

    def test_dormant_producer_with_a_re_arm_clause_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "operator-go:MemoryPostgresWALArchiveBacklog cnpg_collector_pg_wal_archive_status "
                "# DORMANT-PRODUCER: never generated on this cluster. "
                "re-arm: spec.memory.enabled: true on any Project\n",
            )
            waivers = load_waivers(path)
            waiver = waivers[
                (
                    "operator-go",
                    "MemoryPostgresWALArchiveBacklog",
                    "cnpg_collector_pg_wal_archive_status",
                )
            ]
            self.assertEqual(waiver.klass, "DORMANT-PRODUCER")
            self.assertIn("spec.memory.enabled: true", waiver.rearm)

    def test_dormant_producer_is_rejected_on_a_chart_plane(self):
        # A chart file is a SPECIFICATION, not a producer: it is never generated at
        # all, so "the producer is dormant" is never the reason a chart condition is
        # unwatched here.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "operator-chart:TataraBeta operator_beta_total "
                "# DORMANT-PRODUCER: off. re-arm: something\n",
            )
            with self.assertRaises(ValueError):
                load_waivers(path)

    def test_superseded_is_rejected_on_the_go_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp, "operator-go:MemoryDown up # SUPERSEDED: replaced by Foo\n"
            )
            with self.assertRaises(ValueError):
                load_waivers(path)

    def test_an_unknown_class_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp, "operator-chart:TataraBeta operator_beta_total # WHATEVER: why\n"
            )
            with self.assertRaises(ValueError):
                load_waivers(path)


class LiveTreeTest(unittest.TestCase):
    """The shipped waiver file must parse, and must not carry a waiver for a metric that
    alerts/ actually reads - a waiver that has become unnecessary is stale documentation
    claiming a gap that is closed."""

    def test_shipped_waivers_parse(self):
        root = pathlib.Path(__file__).resolve().parent
        waivers = load_waivers(root / "alert_plane_waivers.txt")
        self.assertTrue(waivers)
        for (plane, alert, metric), waiver in waivers.items():
            self.assertTrue(waiver.reason, f"{plane}:{alert}/{metric} has an empty reason")

    def test_no_shipped_waiver_covers_a_metric_alerts_already_read(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        paths = sorted(str(p) for p in (root / "alerts").glob("*.yaml"))
        read = ported_metrics(paths)
        redundant = [
            f"{plane}:{alert}/{metric}"
            for (plane, alert, metric) in load_waivers(
                root / "scripts" / "alert_plane_waivers.txt"
            )
            if metric in read
        ]
        self.assertEqual(redundant, [])


if __name__ == "__main__":
    unittest.main()
