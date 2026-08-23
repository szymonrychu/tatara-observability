#!/usr/bin/env python3
"""Tests for check_series_liveness. Run: python3 -m unittest scripts.test_check_series_liveness
or, from the scripts/ dir: python3 -m unittest test_check_series_liveness.

No network: every test injects a fake query function.
"""

import pathlib
import tempfile
import unittest

from check_series_liveness import (
    Selector,
    CheckError,
    classify,
    render,
    rule_selectors,
    selectors_in_file,
)


def _fake_query(counts):
    """A query fn returning counts[promql], raising CheckError for anything else."""

    def q(promql):
        if promql not in counts:
            raise CheckError(f"unexpected query {promql!r}")
        return counts[promql]

    return q


class RuleSelectorsTest(unittest.TestCase):
    def test_extracts_the_selector_as_written_not_just_the_name(self):
        rule = {
            "name": "Wrapper turns erroring",
            "queries": [
                {
                    "expression": 'sum(rate(ccw_turns_total{result="failed"}[30m])) / '
                    "clamp_min(sum(rate(ccw_turns_total[30m])), 0.0001)"
                }
            ],
        }
        got = rule_selectors(rule)
        self.assertEqual(
            {s.promql for s in got},
            {'ccw_turns_total{result="failed"}', "ccw_turns_total"},
        )
        self.assertEqual({s.rule for s in got}, {"Wrapper turns erroring"})

    def test_bare_metric_yields_the_bare_name(self):
        rule = {
            "name": "r",
            "queries": [{"expression": "sum(increase(ccw_probe_outcomes_total[1h]))"}],
        }
        self.assertEqual(
            [s.promql for s in rule_selectors(rule)], ["ccw_probe_outcomes_total"]
        )

    def test_histogram_suffixes_keep_their_suffix(self):
        # Unlike the provenance check, liveness asks about the SERIES: the base
        # name of a histogram has no series of its own, _count does.
        rule = {
            "name": "r",
            "queries": [{"expression": "sum(rate(ccw_turn_duration_seconds_count[5m]))"}],
        }
        self.assertEqual(
            [s.promql for s in rule_selectors(rule)], ["ccw_turn_duration_seconds_count"]
        )

    def test_skips_loki_queries(self):
        rule = {
            "name": "r",
            "queries": [
                {"query_type": "loki", "expression": '{app="x"} |= "boom"'},
                {"expression": "sum(operator_tasks_total)"},
            ],
        }
        self.assertEqual([s.promql for s in rule_selectors(rule)], ["operator_tasks_total"])

    def test_skips_selectors_carrying_a_grafana_template_variable(self):
        rule = {
            "name": "r",
            "queries": [{"expression": 'sum(operator_tasks_total{project="$project"})'}],
        }
        self.assertEqual([s.promql for s in rule_selectors(rule)], [])

    def test_deduplicates_within_a_rule(self):
        rule = {
            "name": "r",
            "queries": [
                {"expression": "sum(rate(a_total[5m]))"},
                {"expression": "sum(rate(a_total[1h]))"},
            ],
        }
        self.assertEqual([s.promql for s in rule_selectors(rule)], ["a_total"])


class SelectorsInFileTest(unittest.TestCase):
    def test_walks_every_rule_in_an_alert_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "a.yaml"
            p.write_text(
                "rules:\n"
                '  - name: "one"\n'
                "    queries:\n"
                '      - expression: |\n'
                '          sum(rate(x_total{result="fail"}[5m]))\n'
                '  - name: "two"\n'
                "    queries:\n"
                "      - expression: |\n"
                "          sum(y_total)\n"
            )
            got = selectors_in_file(str(p))
        self.assertEqual(
            [(s.rule, s.promql) for s in got],
            [("one", 'x_total{result="fail"}'), ("two", "y_total")],
        )


class ClassifyTest(unittest.TestCase):
    def test_zero_series_is_dark_and_a_positive_count_is_live(self):
        sels = [
            Selector("a.yaml", "r1", 'ccw_commit_push_total{result="fail"}'),
            Selector("a.yaml", "r2", "ccw_commit_push_total"),
        ]
        results = classify(
            sels,
            _fake_query(
                {
                    'count(ccw_commit_push_total{result="fail"})': 0,
                    "count(ccw_commit_push_total)": 2,
                }
            ),
        )
        self.assertEqual([r.count for r in results], [0, 2])
        self.assertEqual([r.dark for r in results], [True, False])

    def test_a_query_failure_is_not_a_dark_metric(self):
        # A check that cannot run must say so, not report the fleet as healthy.
        sels = [Selector("a.yaml", "r", "x_total")]
        with self.assertRaises(CheckError):
            classify(sels, _fake_query({}))


class RenderTest(unittest.TestCase):
    def test_names_every_dark_selector_and_its_rule(self):
        results = classify(
            [
                Selector("alerts/w.yaml", "Wrapper turns erroring", 'ccw_turns_total{result="failed"}'),
                Selector("alerts/w.yaml", "Wrapper turns erroring", "ccw_turns_total"),
            ],
            _fake_query(
                {
                    'count(ccw_turns_total{result="failed"})': 0,
                    "count(ccw_turns_total)": 2,
                }
            ),
        )
        out = render(results)
        self.assertIn("DARK", out)
        self.assertIn('ccw_turns_total{result="failed"}', out)
        self.assertIn("Wrapper turns erroring", out)
        self.assertIn("1 of 2", out)

    def test_an_all_live_fleet_says_so(self):
        results = classify(
            [Selector("alerts/w.yaml", "r", "x_total")],
            _fake_query({"count(x_total)": 3}),
        )
        out = render(results)
        self.assertNotIn("DARK", out)


class ExitPolicyTest(unittest.TestCase):
    """Dark metrics are a FLEET condition: reported, never gated. Only an
    inability to run the check is an error, so a check that has silently
    stopped asserting is visible."""

    def test_dark_metrics_exit_zero(self):
        rc = run_over(
            {'count(x_total{result="fail"})': 0, "count(x_total)": 0},
            'sum(rate(x_total{result="fail"}[5m])) / sum(rate(x_total[5m]))',
        )
        self.assertEqual(rc, 0)

    def test_a_query_failure_exits_nonzero(self):
        rc = run_over({}, "sum(x_total)")
        self.assertNotEqual(rc, 0)


def run_over(counts, expression):
    """Run check_series_liveness.run() over a one-rule alert file with a fake query fn."""
    import check_series_liveness as check

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "a.yaml"
        p.write_text(
            'rules:\n  - name: "r"\n    queries:\n      - expression: |\n          '
            + expression
            + "\n"
        )
        return check.run([str(p)], _fake_query(counts))


if __name__ == "__main__":
    unittest.main()
