#!/usr/bin/env python3
"""Tests for check_metric_provenance. Run: python3 -m unittest scripts.test_check_metric_provenance
or, from the scripts/ dir: python3 -m unittest test_check_metric_provenance."""

import json
import pathlib
import tempfile
import unittest

from check_metric_provenance import (
    alert_queries,
    dashboard_queries,
    iter_expressions,
    lint_dashboard,
    lint_rule,
    metric_names,
    selector_labels,
    template_expr,
)


class MetricNamesTest(unittest.TestCase):
    def test_extracts_bare_and_selected_metrics(self):
        expr = (
            'sum(increase(operator_task_parked_total{stage="failed"}[1h])) or vector(0)'
        )
        self.assertEqual(metric_names(expr), {"operator_task_parked_total"})

    def test_ignores_promql_functions_and_keywords(self):
        expr = "histogram_quantile(0.95, sum(rate(operator_bundle_bytes_bucket[15m])) by (le)) and on() (time() > 0)"
        # The _bucket suffix is stripped to the base metric name (see
        # test_histogram_suffixes_resolve_to_the_base_metric): the allowlist lists
        # base names only, so this must be consistent with that test.
        self.assertEqual(metric_names(expr), {"operator_bundle_bytes"})

    def test_ignores_label_values_and_durations(self):
        expr = 'max by (task) (operator_task_stage_age_seconds{stage=~"merging|deploying"})'
        self.assertEqual(metric_names(expr), {"operator_task_stage_age_seconds"})

    def test_histogram_suffixes_resolve_to_the_base_metric(self):
        expr = "sum(increase(operator_tasks_minted_per_sweep_sum[1h]))"
        self.assertEqual(metric_names(expr), {"operator_tasks_minted_per_sweep"})

    def test_ignores_join_modifier_label_lists(self):
        # on(...)/ignoring(...)/group_left(...) carry label lists, not metrics -
        # a real bug found against alerts/tatara-wrapper.yaml's "not becoming
        # ready" rule, which would otherwise report a phantom `pod`/`namespace`
        # "metric".
        expr = (
            'sum(kube_pod_status_ready{namespace="tatara",condition="false"} '
            "* on(pod,namespace) group_left() "
            'max(kube_pod_container_status_waiting_reason{namespace="tatara"}) '
            "by (pod,namespace))"
        )
        self.assertEqual(
            metric_names(expr),
            {"kube_pod_status_ready", "kube_pod_container_status_waiting_reason"},
        )


class LintRuleTest(unittest.TestCase):
    def test_unknown_metric_is_a_violation(self):
        rule = {
            "name": "dead",
            "queries": [{"expression": 'max(tatara_cd_cascade_failed{job="x"})'}],
        }
        v = lint_rule("alerts/x.yaml", rule, {"operator_task_stage"})
        self.assertIsNotNone(v)
        self.assertIn("tatara_cd_cascade_failed", str(v))

    def test_known_metric_is_clean(self):
        rule = {
            "name": "live",
            "queries": [{"expression": 'max(operator_task_stage{stage="failed"})'}],
        }
        self.assertIsNone(lint_rule("alerts/x.yaml", rule, {"operator_task_stage"}))

    def test_loki_queries_are_skipped(self):
        rule = {
            "name": "log",
            "queries": [
                {
                    "expression": 'sum(count_over_time({namespace="tatara", app="tatara-operator"} | json | level="ERROR" [5m]))',
                    "query_type": "loki",
                }
            ],
        }
        self.assertIsNone(lint_rule("alerts/x.yaml", rule, set()))


class SelectorLabelsTest(unittest.TestCase):
    """selector_labels reports EVERY label a selector names, not a fixed four.

    The pre-#100 extractor hardcoded stageReason|stage|kind|agent_kind, so the
    post-v2.0.0 vocabulary (state, stateReason, parkReason, park_reason) was
    invisible to it, and no label name was ever validated at all. The consumer
    (reconcile_metric_provenance) decides which labels it can say anything about;
    this function just reports what the expression asks for.
    """

    def test_reports_every_label_with_its_operator_and_metric(self):
        expr = 'max(operator_task_terminal_total{state=~"done|rejected",kind="refine"})'
        self.assertEqual(
            sorted(selector_labels(expr)),
            [
                ("operator_task_terminal_total", "kind", "=", frozenset({"refine"})),
                (
                    "operator_task_terminal_total",
                    "state",
                    "=~",
                    frozenset({"done", "rejected"}),
                ),
            ],
        )

    def test_reports_labels_the_old_four_name_regex_could_not_see(self):
        expr = 'sum(operator_task_parked_total{parkReason="merge-blocked",state="merged"})'
        self.assertEqual(
            {s.label for s in selector_labels(expr)}, {"parkReason", "state"}
        )

    def test_negative_matchers_are_reported_and_flagged_as_such(self):
        # `!=` was never matched by the pre-#100 regex at all: `!~?` consumes the
        # "!" and then requires a quote, so `label!="v"` fell through silently.
        found = sorted(selector_labels('sum(m{a!="x",b!~"y",c="z"})'))
        self.assertEqual([s.op for s in found], ["!=", "!~", "="])
        self.assertEqual([s.positive for s in found], [False, False, True])

    def test_only_regex_operators_split_on_the_alternation_bar(self):
        # A literal `=` value is one value even if it contains a bar.
        self.assertEqual(
            selector_labels('sum(m{a="x|y"})')[0].values, frozenset({"x|y"})
        )
        self.assertEqual(
            selector_labels('sum(m{a=~"x|y"})')[0].values, frozenset({"x", "y"})
        )

    def test_grafana_template_variables_are_dropped(self):
        # kind=~"$kind" is a dashboard variable, not a literal label value. The
        # LABEL is still reported - a variable-valued matcher on a dead label name
        # is exactly as dark as a literal one.
        found = selector_labels('sum by (kind) (operator_task_terminal_total{kind=~"$kind"})')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].values, frozenset())

    def test_histogram_suffix_resolves_to_the_base_metric(self):
        expr = 'histogram_quantile(0.95, rate(operator_bundle_bytes_bucket{agent_kind="review"}[5m]))'
        self.assertEqual(
            selector_labels(expr),
            [("operator_bundle_bytes", "agent_kind", "=", frozenset({"review"}))],
        )

    def test_a_selector_with_no_labels_reports_nothing(self):
        self.assertEqual(selector_labels("sum(operator_task_terminal_total)"), [])

    def test_a_grouping_clause_is_not_a_label_matcher(self):
        # `by (le)` carries a label LIST, not a matcher, and lives outside any
        # `{...}` body - so it must not be reported as a selected label.
        self.assertEqual(
            selector_labels(
                "histogram_quantile(0.95, sum(rate(m_bucket[5m])) by (le, route))"
            ),
            [],
        )

    def test_matching_starts_on_an_identifier_boundary(self):
        # Without the boundary guard the matcher could start mid-identifier and
        # report a truncated label name, which would read as an undeclared label.
        self.assertEqual(
            [s.label for s in selector_labels('sum(m{stageReason="x",agent_kind="y"})')],
            ["stageReason", "agent_kind"],
        )


class TemplateExprTest(unittest.TestCase):
    def test_label_values_with_a_metric_yields_the_metric(self):
        self.assertEqual(
            template_expr(
                'label_values(operator_task_stage{stage="failed"}, kind)'
            ).strip(),
            'operator_task_stage{stage="failed"}',
        )

    def test_label_values_without_a_metric_yields_nothing(self):
        self.assertEqual(template_expr("label_values(namespace)").strip(), "")

    def test_metric_and_label_name_queries_yield_nothing(self):
        self.assertEqual(template_expr("metrics(operator_.*)").strip(), "")
        self.assertEqual(template_expr("label_names()").strip(), "")

    def test_query_result_yields_its_inner_expression(self):
        self.assertEqual(
            template_expr("query_result(up{job='tatara-operator'})").strip(),
            "up{job='tatara-operator'}",
        )


def _write_dashboard(tmp: str, dashboard: dict) -> str:
    path = pathlib.Path(tmp) / "d.json"
    path.write_text(json.dumps(dashboard))
    return str(path)


_DASHBOARD = {
    "panels": [
        {
            "title": "live",
            "targets": [{"expr": "sum by (stage) (operator_task_stage)"}],
        },
        {
            "title": "logs",
            "targets": [
                {
                    "datasource": {"type": "loki", "uid": "loki"},
                    "expr": 'sum(count_over_time({app="tatara-operator"}[5m]))',
                }
            ],
        },
        {
            "title": "row",
            "type": "row",
            "panels": [
                {
                    "title": "nested dead",
                    "targets": [
                        {"expr": "sum(increase(tatara_cd_resolved_total[1h]))"}
                    ],
                }
            ],
        },
    ],
    "templating": {
        "list": [
            {
                "type": "query",
                "name": "kind",
                "query": {"query": "label_values(tatara_issue_state, kind)"},
            },
            {"type": "datasource", "name": "ds", "query": "prometheus"},
        ]
    },
}


class DashboardQueriesTest(unittest.TestCase):
    def test_walks_panels_rows_and_template_variables_and_skips_loki(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = dashboard_queries(_write_dashboard(tmp, _DASHBOARD))
        self.assertEqual(
            found,
            [
                ('panel "live"', "sum by (stage) (operator_task_stage)"),
                ('panel "nested dead"', "sum(increase(tatara_cd_resolved_total[1h]))"),
                ('variable "kind"', "tatara_issue_state"),
            ],
        )


class LintDashboardTest(unittest.TestCase):
    def test_dead_metric_in_a_row_collapsed_panel_is_a_violation(self):
        # The whole point of the 2026-07-13 extension: a panel on a deleted metric renders
        # empty forever with no CI signal. A nested (row) panel must not escape the sweep.
        with tempfile.TemporaryDirectory() as tmp:
            violations = lint_dashboard(
                _write_dashboard(tmp, _DASHBOARD), {"operator_task_stage"}
            )
        rendered = [str(v) for v in violations]
        self.assertEqual(len(rendered), 2, rendered)
        self.assertIn("tatara_cd_resolved_total", rendered[0])
        self.assertIn('panel "nested dead"', rendered[0])
        self.assertIn("tatara_issue_state", rendered[1])
        self.assertIn('variable "kind"', rendered[1])

    def test_clean_dashboard_is_clean(self):
        clean = {
            "panels": [
                {
                    "title": "live",
                    "targets": [{"expr": 'sum(operator_task_stage{stage="merging"})'}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                lint_dashboard(_write_dashboard(tmp, clean), {"operator_task_stage"}), []
            )


_ALERT_YAML = """\
rules:
  - name: "prom rule"
    queries:
      - expression: 'sum(operator_task_terminal_total{state="done"})'
  - name: "loki rule"
    queries:
      - query_type: loki
        expression: 'sum(count_over_time({app="x"}[5m]))'
"""


class AlertQueriesTest(unittest.TestCase):
    def test_walks_prometheus_queries_and_skips_loki(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "a.yaml"
            p.write_text(_ALERT_YAML)
            self.assertEqual(
                alert_queries(str(p)),
                [('rule "prom rule"', 'sum(operator_task_terminal_total{state="done"})')],
            )


class IterExpressionsTest(unittest.TestCase):
    def test_dispatches_on_suffix_so_reconcile_reuses_one_walk(self):
        # reconcile_metric_provenance's label checks must not re-implement the
        # alert-YAML and dashboard-JSON walks: two walks that drift are two
        # different definitions of "every expression this repo ships".
        with tempfile.TemporaryDirectory() as tmp:
            alert = pathlib.Path(tmp) / "a.yaml"
            alert.write_text(_ALERT_YAML)
            dash = _write_dashboard(tmp, _DASHBOARD)
            found = list(iter_expressions([str(alert), dash]))
        self.assertEqual(
            [(pathlib.Path(p).name, ctx) for p, ctx, _ in found],
            [
                ("a.yaml", 'rule "prom rule"'),
                ("d.json", 'panel "live"'),
                ("d.json", 'panel "nested dead"'),
                ("d.json", 'variable "kind"'),
            ],
        )


if __name__ == "__main__":
    unittest.main()
