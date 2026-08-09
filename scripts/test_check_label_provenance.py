#!/usr/bin/env python3
"""Tests for check_label_provenance. Run: python3 -m unittest
scripts.test_check_label_provenance or, from the scripts/ dir:
python3 -m unittest test_check_label_provenance.

No network calls: derive_metric_labels is exercised against synthetic .go
fixtures written to a tempdir, never a real clone."""

import json
import pathlib
import tempfile
import unittest

import check_metric_provenance
from check_label_provenance import (
    _SUFFIXES,
    Violation,
    check_expr,
    check_paths,
    derive_metric_labels,
    grouping_labels,
    selector_labels,
)

# The v2.0.0 label sets, as tatara-operator's internal/obs declares them. The
# `stage` label is gone from every one of these; `operator_task_parked_total`
# carries parkReason, not stageReason.
_DECLARED = {
    "operator_task_terminal_total": {"kind", "state", "stateReason"},
    "operator_task_parked_total": {"state", "parkReason"},
    "operator_queue_depth": {"class"},
}
_SECTIONS = {
    "operator_task_terminal_total": "operator",
    "operator_task_parked_total": "operator",
    "operator_queue_depth": "operator",
    "operator_task_stage": "operator",
    "operator_dynamic_desc": "operator",
    "kube_pod_container_status_running": "external",
}


class DeriveMetricLabelsTest(unittest.TestCase):
    def _write(self, tmp: pathlib.Path, rel: str, content: str) -> None:
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def _derive(self, rel: str, content: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(root, rel, content)
            return derive_metric_labels(root)

    def test_reads_the_label_slice_off_a_counter_vec(self):
        labels, unresolved = self._derive(
            "internal/obs/task_metrics.go",
            "var x = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
            '    Name: "operator_task_terminal_total",\n'
            '    Help: "h",\n'
            '}, []string{"kind", "state", "stateReason"})\n',
        )
        self.assertEqual(
            labels, {"operator_task_terminal_total": {"kind", "state", "stateReason"}}
        )
        self.assertEqual(unresolved, set())

    def test_a_help_string_with_parens_does_not_truncate_the_call(self):
        # The reason this parses paren-matched instead of using a fixed line
        # window: the label slice sits AFTER a Help string that can run to
        # several concatenated lines and contain parentheses and escaped quotes.
        labels, _ = self._derive(
            "internal/obs/task_metrics.go",
            "var x = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
            '    Name: "operator_task_parked_total",\n'
            '    Help: "Parks (contract K.1), by the STATE the Task was in " +\n'
            '        "when it parked (never on a mint) and the \\"parkReason\\".",\n'
            '}, []string{"state", "parkReason"})\n',
        )
        self.assertEqual(
            labels, {"operator_task_parked_total": {"state", "parkReason"}}
        )

    def test_an_unlabelled_collector_declares_the_empty_set(self):
        # NOT unresolved: a plain NewGauge is a single series with no variable
        # labels, so the empty set is the correct, provable answer.
        labels, unresolved = self._derive(
            "internal/obs/accountusage_metrics.go",
            "var x = prometheus.NewGauge(prometheus.GaugeOpts{\n"
            '    Name: "tatara_account_usage_poll_health",\n'
            '    Help: "h",\n'
            "})\n",
        )
        self.assertEqual(labels, {"tatara_account_usage_poll_health": set()})
        self.assertEqual(unresolved, set())

    def test_new_desc_positional_name_with_variable_labels(self):
        labels, _ = self._derive(
            "internal/pushmetrics/receiver.go",
            "d := prometheus.NewDesc(\n"
            '    "operator_pushed_runs", "help", []string{"project"}, nil,\n'
            ")\n",
        )
        self.assertEqual(labels, {"operator_pushed_runs": {"project"}})

    def test_new_desc_with_nil_variable_labels_declares_the_empty_set(self):
        labels, unresolved = self._derive(
            "internal/pushmetrics/receiver.go",
            'd := prometheus.NewDesc("operator_pushed_total", "help", nil, nil)\n',
        )
        self.assertEqual(labels, {"operator_pushed_total": set()})
        self.assertEqual(unresolved, set())

    def test_const_labels_count_as_declared_labels(self):
        labels, _ = self._derive(
            "internal/obs/own_metrics.go",
            "var x = prometheus.NewGaugeVec(prometheus.GaugeOpts{\n"
            '    Name: "operator_build_info",\n'
            '    ConstLabels: prometheus.Labels{"component": "operator"},\n'
            '}, []string{"version"})\n',
        )
        self.assertEqual(labels, {"operator_build_info": {"version", "component"}})

    def test_a_label_slice_built_from_a_variable_is_unresolved_not_empty(self):
        # Fail closed: "I could not read the labels" must never render as "this
        # metric has no labels", which would let every selector on it pass.
        labels, unresolved = self._derive(
            "internal/obs/dynamic.go",
            "var x = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
            '    Name: "operator_dynamic_desc",\n'
            '    Help: "h",\n'
            "}, dynamicLabels)\n",
        )
        self.assertEqual(labels, {})
        self.assertEqual(unresolved, {"operator_dynamic_desc"})

    def test_a_name_resolved_at_any_site_is_not_reported_unresolved(self):
        labels, unresolved = self._derive(
            "internal/obs/both.go",
            "var a = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
            '    Name: "operator_two_sites", Help: "h",\n'
            "}, dynamicLabels)\n"
            "var b = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
            '    Name: "operator_two_sites", Help: "h",\n'
            '}, []string{"kind"})\n',
        )
        self.assertEqual(labels, {"operator_two_sites": {"kind"}})
        self.assertEqual(unresolved, set())

    def test_ignores_test_files_and_kubernetes_struct_literals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(
                root,
                "internal/obs/task_metrics_test.go",
                "prometheus.NewCounterVec(prometheus.CounterOpts{\n"
                '    Name: "only_in_tests",\n'
                '}, []string{"x"})\n',
            )
            self._write(
                root,
                "internal/pod/spec.go",
                'p := corev1.ContainerPort{Name: "http", ContainerPort: 8080}\n',
            )
            labels, unresolved = derive_metric_labels(root)
        self.assertEqual(labels, {})
        self.assertEqual(unresolved, set())


class SelectorLabelsTest(unittest.TestCase):
    def test_reads_every_matcher_including_not_equal(self):
        expr = (
            'operator_task_parked_total{namespace="tatara",parkReason=~"a|b",'
            'kind!="x",state!~"y"}'
        )
        self.assertEqual(
            selector_labels(expr),
            {
                "operator_task_parked_total": {
                    "namespace",
                    "parkReason",
                    "kind",
                    "state",
                }
            },
        )

    def test_histogram_suffix_resolves_to_the_declared_base_name(self):
        expr = 'operator_turn_duration_seconds_bucket{kind="review",le="5"}'
        self.assertEqual(
            selector_labels(expr),
            {"operator_turn_duration_seconds": {"kind", "le"}},
        )

    def test_each_metric_carries_only_its_own_labels(self):
        expr = 'operator_a{alpha="1"} / operator_b{beta="2"}'
        self.assertEqual(
            selector_labels(expr), {"operator_a": {"alpha"}, "operator_b": {"beta"}}
        )


class GroupingLabelsTest(unittest.TestCase):
    def test_binds_a_by_clause_to_the_single_metric_it_groups(self):
        expr = (
            'sum by (parkReason) (increase(operator_task_parked_total{job="x"}[1h]))'
            " or vector(0)"
        )
        self.assertEqual(
            grouping_labels(expr), {"operator_task_parked_total": {"parkReason"}}
        )

    def test_skips_a_multi_metric_expression(self):
        # No single owner for the grouping label, and guessing one is how a
        # guard earns the false positives that get it switched off.
        expr = "sum by (project) (operator_a) / sum by (project) (operator_b)"
        self.assertEqual(grouping_labels(expr), {})

    def test_skips_an_expression_that_mints_labels(self):
        expr = 'sum by (project) (label_replace(operator_a, "project", "$1", "pod", "(.*)"))'
        self.assertEqual(grouping_labels(expr), {})

    def test_without_is_not_checked(self):
        # Removing a label that is not there is a genuine no-op, and defensive
        # `without (le)` is idiomatic - see the module docstring.
        expr = "sum without (le) (rate(operator_a_bucket[5m]))"
        self.assertEqual(grouping_labels(expr), {})


class CheckExprTest(unittest.TestCase):
    def _check(self, expr):
        return [
            str(v)
            for v in check_expr(
                "alerts/x.yaml", 'rule "r"', expr, _SECTIONS, _DECLARED, set()
            )
        ]

    def test_issue_100_the_stage_selector_on_a_surviving_metric(self):
        # THE regression this script exists for. The metric is still emitted,
        # so check_metric_provenance's name check passes; `stage` was one of the
        # four label names its VALUE sweep knew and "failed" was a member of the
        # stale closed set, so that passed too. Five rules matched nothing
        # forever with every check green.
        found = self._check(
            'sum(increase(operator_task_terminal_total{namespace="tatara",'
            'stage="failed"}[1h])) or vector(0)'
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("names label `stage`", found[0])
        self.assertIn("operator_task_terminal_total", found[0])

    def test_a_negative_selector_on_a_missing_label_is_also_caught(self):
        # The mirror image: `stageReason!~"..."` on a metric with no stageReason
        # matches EVERY series, so the rule goes falsely noisy rather than dark.
        found = self._check(
            'sum(increase(operator_task_parked_total{stageReason!~"backlog-sweep"}[1h]))'
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("names label `stageReason`", found[0])

    def test_a_grouping_label_the_metric_does_not_carry_is_caught(self):
        found = self._check(
            'sum by (kind) (increase(operator_task_parked_total'
            '{parkReason="triage-stalled"}[1h])) or vector(0)'
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("names label `kind`", found[0])

    def test_a_correct_expression_is_clean(self):
        self.assertEqual(
            self._check(
                'sum by (parkReason) (increase(operator_task_parked_total{namespace="tatara",'
                'job="tatara-operator",parkReason=~"merge-timeout|deploy-timeout"}'
                " [30m])) or vector(0)"
            ),
            [],
        )

    def test_scrape_pipeline_labels_are_legal_on_every_metric(self):
        # job/namespace/pod/container/node/le are attached by the scrape config
        # or synthesised by the client library, never by the producer's
        # []string{...}, so they must not be flagged.
        self.assertEqual(
            self._check(
                'operator_queue_depth{job="tatara-operator",namespace="tatara",'
                'pod="p",container="c",node="n",instance="i",class="alert"}'
            ),
            [],
        )

    def test_honor_labels_collision_prefix_is_legal(self):
        self.assertEqual(self._check('operator_queue_depth{exported_pod="p"}'), [])

    def test_the_external_section_is_exempt_and_says_so_in_one_place(self):
        # kube-state-metrics is emitted by no tatara repo, so there is no source
        # to derive from. The exemption is the SECTION_REPO mapping the
        # reconcile script already owns, not a second hand-maintained list.
        self.assertEqual(
            self._check('kube_pod_container_status_running{container="wrapper"}'), []
        )

    def test_an_unallowlisted_metric_fails_closed(self):
        found = self._check('operator_never_heard_of_it{kind="x"}')
        self.assertEqual(len(found), 1, found)
        self.assertIn("not in scripts/metrics_allowlist.txt", found[0])

    def test_a_metric_no_producer_declares_fails_closed(self):
        # operator_task_stage is allowlisted but v2.0.0 renamed it, so no clone
        # declares it. Reporting OK here is precisely the silent-green failure.
        found = self._check('operator_task_stage{state="new"}')
        self.assertEqual(len(found), 1, found)
        self.assertIn("declares nowhere in its Go source", found[0])

    def test_an_unresolvable_label_slice_fails_closed(self):
        found = [
            str(v)
            for v in check_expr(
                "alerts/x.yaml",
                'rule "r"',
                'operator_dynamic_desc{kind="x"}',
                _SECTIONS,
                _DECLARED,
                {"operator_dynamic_desc"},
            )
        ]
        self.assertEqual(len(found), 1, found)
        self.assertIn("builds from a variable", found[0])


class CheckPathsTest(unittest.TestCase):
    def _alert(self, tmp, rules):
        path = pathlib.Path(tmp) / "alerts.yaml"
        path.write_text(json.dumps({"rules": rules}))  # JSON is valid YAML
        return str(path)

    def _dashboard(self, tmp, doc):
        path = pathlib.Path(tmp) / "board.json"
        path.write_text(json.dumps(doc))
        return str(path)

    def test_walks_alert_rules_and_skips_loki_queries(self):
        rules = [
            {
                "name": "dark",
                "queries": [
                    {"expression": 'operator_task_terminal_total{stage="failed"}'}
                ],
            },
            {
                "name": "logs",
                "queries": [
                    {
                        "query_type": "loki",
                        "expression": '{namespace="tatara", stage="failed"}',
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            found = check_paths(
                [self._alert(tmp, rules)], _SECTIONS, _DECLARED, set()
            )
        self.assertEqual(len(found), 1, [str(v) for v in found])
        self.assertIn('rule "dark"', str(found[0]))

    def test_walks_dashboard_panels_including_row_collapsed_ones(self):
        doc = {
            "panels": [
                {
                    "title": "row",
                    "panels": [
                        {
                            "title": "nested dark",
                            "targets": [
                                {
                                    "expr": 'sum by (stageReason) (operator_task_parked_total)'
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            found = check_paths(
                [self._dashboard(tmp, doc)], _SECTIONS, _DECLARED, set()
            )
        self.assertEqual(len(found), 1, [str(v) for v in found])
        self.assertIn('panel "nested dark"', str(found[0]))
        self.assertIn("names label `stageReason`", str(found[0]))

    def test_repeated_findings_are_deduplicated(self):
        doc = {
            "panels": [
                {
                    "title": "twice",
                    "targets": [
                        {"expr": 'operator_task_terminal_total{stage="failed"}'},
                        {"expr": 'operator_task_terminal_total{stage="parked"}'},
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            found = check_paths(
                [self._dashboard(tmp, doc)], _SECTIONS, _DECLARED, set()
            )
        self.assertEqual(len(found), 1, [str(v) for v in found])


class ContractTest(unittest.TestCase):
    def test_suffix_handling_agrees_with_check_metric_provenance(self):
        # Both scripts strip the same query-side suffixes to reach the name the
        # producer declares. A drift between them would silently un-check every
        # histogram and summary in the repo.
        self.assertEqual(_SUFFIXES, check_metric_provenance._SUFFIXES)

    def test_every_violation_kind_renders_a_distinct_actionable_message(self):
        rendered = {
            kind: str(Violation("p", "c", kind, "m", "d"))
            for kind in ("label", "unallowlisted", "underivable", "unresolved")
        }
        self.assertEqual(len(set(rendered.values())), 4, rendered)
        for kind, text in rendered.items():
            self.assertTrue(text.startswith("p: c "), (kind, text))


if __name__ == "__main__":
    unittest.main()
