#!/usr/bin/env python3
"""Tests for check_metric_provenance. Run: python3 -m unittest scripts.test_check_metric_provenance
or, from the scripts/ dir: python3 -m unittest test_check_metric_provenance."""

import json
import pathlib
import tempfile
import unittest

from check_metric_provenance import (
    dashboard_queries,
    label_values,
    lint_dashboard,
    lint_rule,
    load_stage_values,
    metric_names,
    template_expr,
)

_STAGE_VALUES = {
    "stage": {"failed", "merging", "deploying"},
    "stageReason": {"merge-blocked", "head-moving"},
    "kind": {"clarify", "review"},
    "agent_kind": {"implement", "review"},
    "kind:exempt-metrics": {"operator_scm_writes_total"},
}


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


class LabelValuesTest(unittest.TestCase):
    def test_extracts_single_and_alternation_values_per_metric(self):
        expr = 'max(operator_task_stage{stage=~"merging|deploying",kind="clarify"})'
        self.assertEqual(
            label_values(expr),
            {
                "operator_task_stage": {
                    "stage": {"merging", "deploying"},
                    "kind": {"clarify"},
                }
            },
        )

    def test_ignores_labels_outside_the_tracked_set(self):
        expr = 'max(operator_task_stage{stage="failed",namespace="tatara"})'
        self.assertEqual(
            label_values(expr), {"operator_task_stage": {"stage": {"failed"}}}
        )

    def test_ignores_grafana_template_variable_values(self):
        # kind=~"$kind" is a dashboard variable, not a literal label value.
        expr = 'sum by (kind) (operator_task_terminal_total{kind=~"$kind"})'
        self.assertEqual(label_values(expr), {})

    def test_histogram_suffix_resolves_to_the_base_metric(self):
        expr = 'histogram_quantile(0.95, rate(operator_bundle_bytes_bucket{agent_kind="review"}[5m]))'
        self.assertEqual(
            label_values(expr), {"operator_bundle_bytes": {"agent_kind": {"review"}}}
        )


class StageValueLintTest(unittest.TestCase):
    def test_dead_stage_reason_is_a_violation(self):
        # pod-not-ready was removed from the F.5 closed set (fix V7-7): a never-
        # Ready pod respawns, it does not terminate. A rule still filtering on it
        # would report OK forever - the metric name is fine, only the value is dead.
        rule = {
            "name": "stale reason",
            "queries": [
                {
                    "expression": 'sum(operator_task_parked_total{stageReason="pod-not-ready"})'
                }
            ],
        }
        v = lint_rule(
            "alerts/x.yaml",
            rule,
            {"operator_task_parked_total"},
            _STAGE_VALUES,
        )
        self.assertIsNotNone(v)
        self.assertIn("pod-not-ready", str(v))

    def test_live_stage_reason_is_clean(self):
        rule = {
            "name": "live reason",
            "queries": [
                {
                    "expression": 'sum(operator_task_parked_total{stageReason="merge-blocked"})'
                }
            ],
        }
        self.assertIsNone(
            lint_rule(
                "alerts/x.yaml",
                rule,
                {"operator_task_parked_total"},
                _STAGE_VALUES,
            )
        )

    def test_stage_value_sweep_is_opt_in(self):
        # Callers that pass no stage_values argument (e.g. the plan's original
        # three-positional-arg call sites) get metric-name checking only - the
        # value sweep must not break that contract.
        rule = {
            "name": "live reason",
            "queries": [
                {
                    "expression": 'sum(operator_task_parked_total{stageReason="pod-not-ready"})'
                }
            ],
        }
        self.assertIsNone(
            lint_rule("alerts/x.yaml", rule, {"operator_task_parked_total"})
        )

    def test_overloaded_kind_label_is_exempt_per_metric(self):
        # operator_scm_writes_total{kind="write"} is an ACCESS CLASS, not a Task kind.
        # The exemption is per-metric: the same value on a Task-family metric still fails.
        ok = {
            "name": "scm writes",
            "queries": [
                {"expression": 'sum(rate(operator_scm_writes_total{kind="write"}[5m]))'}
            ],
        }
        self.assertIsNone(
            lint_rule("alerts/x.yaml", ok, {"operator_scm_writes_total"}, _STAGE_VALUES)
        )
        bad = {
            "name": "task stage",
            "queries": [{"expression": 'sum(operator_task_stage{kind="write"})'}],
        }
        v = lint_rule("alerts/x.yaml", bad, {"operator_task_stage"}, _STAGE_VALUES)
        self.assertIsNotNone(v)
        self.assertIn('kind="write"', str(v))


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
                _write_dashboard(tmp, _DASHBOARD),
                {"operator_task_stage"},
                _STAGE_VALUES,
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
                lint_dashboard(
                    _write_dashboard(tmp, clean), {"operator_task_stage"}, _STAGE_VALUES
                ),
                [],
            )


class LoadStageValuesTest(unittest.TestCase):
    def test_parses_sectioned_value_file(self):
        text = "# comment\n## stage\nfailed\nmerging\n\n## stageReason\nmerge-blocked\n"

        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "values.txt"
            p.write_text(text)
            parsed = load_stage_values(str(p))
        self.assertEqual(
            parsed,
            {"stage": {"failed", "merging"}, "stageReason": {"merge-blocked"}},
        )


if __name__ == "__main__":
    unittest.main()
