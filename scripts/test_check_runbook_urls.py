#!/usr/bin/env python3
"""Tests for check_runbook_urls. Run:
python3 -m unittest scripts.test_check_runbook_urls or, from the scripts/ dir:
python3 -m unittest test_check_runbook_urls.

No network calls: the cross-repo half is exercised by feeding parse_declared_anchors a
markdown string, never a real clone."""

import pathlib
import tempfile
import unittest

import yaml

from check_runbook_urls import (
    check_paths,
    check_rule,
    expected_url,
    parse_declared_anchors,
    reconcile,
    slugify,
)


class SlugifyTest(unittest.TestCase):
    def test_lowercases_and_hyphenates(self):
        self.assertEqual(
            slugify("Memory stack stuck not ready"), "memory-stack-stuck-not-ready"
        )

    def test_collapses_runs_of_non_slug_characters(self):
        self.assertEqual(
            slugify("Wrapper commit/push failure ratio high"),
            "wrapper-commit-push-failure-ratio-high",
        )

    def test_strips_leading_and_trailing_separators(self):
        self.assertEqual(slugify("  -- Operator GC blocked -- "), "operator-gc-blocked")

    def test_drops_punctuation_entirely(self):
        self.assertEqual(
            slugify("Agent token spend runaway ($/s)"), "agent-token-spend-runaway-s"
        )

    def test_survives_an_equals_sign_and_a_digit(self):
        self.assertEqual(
            slugify("Tier-quality rubber-stamp (model=claude-sonnet-5)"),
            "tier-quality-rubber-stamp-model-claude-sonnet-5",
        )

    def test_expected_url_is_the_docs_site_anchor(self):
        self.assertEqual(
            expected_url("Operator GC blocked"),
            "https://szymonrychu.github.io/tatara-documentation/operations/runbooks/"
            "#tatara-runbook-operator-gc-blocked",
        )


class CheckRuleTest(unittest.TestCase):
    def _rule(self, url=None, name="Operator GC blocked"):
        annotations = {"summary": "x"}
        if url is not None:
            annotations["runbook_url"] = url
        return {"name": name, "annotations": annotations}

    def test_contract_shaped_url_passes(self):
        rule = self._rule(expected_url("Operator GC blocked"))
        self.assertIsNone(check_rule("alerts/x.yaml", rule))

    def test_missing_annotation_fails(self):
        v = check_rule("alerts/x.yaml", self._rule(None))
        self.assertIsNotNone(v)
        self.assertIn("has no `runbook_url` annotation", str(v))

    def test_empty_annotation_fails(self):
        v = check_rule("alerts/x.yaml", self._rule("   "))
        self.assertIsNotNone(v)
        self.assertIn("has no `runbook_url` annotation", str(v))

    def test_bare_page_url_fails(self):
        # The pre-mortem cheat: satisfy a weaker "is it a docs URL" check by pointing at
        # the page with no fragment. Coverage would read 100% and resolve to nothing.
        v = check_rule(
            "alerts/x.yaml",
            self._rule(
                "https://szymonrychu.github.io/tatara-documentation/operations/runbooks/"
            ),
        )
        self.assertIsNotNone(v)
        self.assertIn("the contract derives", str(v))

    def test_anchor_for_a_different_rule_fails(self):
        v = check_rule("alerts/x.yaml", self._rule(expected_url("Some other rule")))
        self.assertIsNotNone(v)
        self.assertIn("the contract derives", str(v))

    def test_rule_with_no_annotations_block_at_all_fails(self):
        v = check_rule("alerts/x.yaml", {"name": "Operator GC blocked"})
        self.assertIsNotNone(v)
        self.assertIn("has no `runbook_url` annotation", str(v))


class CheckPathsTest(unittest.TestCase):
    def _write(self, body: str) -> str:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        tmp.write(body)
        tmp.close()
        return tmp.name

    def test_collects_anchors_for_clean_files(self):
        path = self._write(
            "rules:\n"
            '  - name: "Operator GC blocked"\n'
            "    annotations:\n"
            f'      runbook_url: "{expected_url("Operator GC blocked")}"\n'
        )
        violations, anchors = check_paths([path])
        self.assertEqual(violations, [])
        self.assertEqual(
            anchors, {"tatara-runbook-operator-gc-blocked": "Operator GC blocked"}
        )

    def test_a_violating_rule_contributes_no_anchor(self):
        path = self._write(
            'rules:\n  - name: "Operator GC blocked"\n    annotations: {}\n'
        )
        violations, anchors = check_paths([path])
        self.assertEqual(len(violations), 1)
        self.assertEqual(anchors, {})

    def test_empty_file_is_not_an_error(self):
        path = self._write("")
        self.assertEqual(check_paths([path]), ([], {}))


class DeclaredAnchorsTest(unittest.TestCase):
    MARKDOWN = (
        '<a id="tatara-runbook-operator-gc-blocked"></a>'
        '<!-- alert: "Operator GC blocked" status: covered -->\n'
        '<a id="tatara-runbook-operator-sweep-erroring"></a>'
        '<!-- alert: "Operator sweep erroring" status: none -->\n'
        "## Ownership or GC invariant broken\n"
    )

    def test_parses_both_statuses(self):
        self.assertEqual(
            parse_declared_anchors(self.MARKDOWN),
            {
                "tatara-runbook-operator-gc-blocked": "covered",
                "tatara-runbook-operator-sweep-erroring": "none",
            },
        )

    def test_ignores_an_anchor_with_no_marker_comment(self):
        self.assertEqual(
            parse_declared_anchors('<a id="tatara-runbook-orphan"></a>\n'), {}
        )

    def test_ignores_an_unrelated_anchor(self):
        self.assertEqual(
            parse_declared_anchors(
                '<a id="some-other-anchor"></a><!-- alert: "x" status: covered -->'
            ),
            {},
        )

    def test_reconcile_reports_only_undeclared_anchors(self):
        anchors = {
            "tatara-runbook-operator-gc-blocked": "Operator GC blocked",
            "tatara-runbook-brand-new-rule": "Brand new rule",
        }
        self.assertEqual(
            reconcile(anchors, parse_declared_anchors(self.MARKDOWN)),
            ["tatara-runbook-brand-new-rule"],
        )

    def test_an_example_inside_a_code_fence_is_not_a_declaration(self):
        fenced = "```html\n" + self.MARKDOWN + "```\n"
        self.assertEqual(parse_declared_anchors(fenced), {})

    def test_a_placeholder_anchor_still_counts_as_declared(self):
        # status: none is an honest "no runbook written yet", not a dangling link.
        anchors = {"tatara-runbook-operator-sweep-erroring": "Operator sweep erroring"}
        self.assertEqual(reconcile(anchors, parse_declared_anchors(self.MARKDOWN)), [])


class LiveAlertsTest(unittest.TestCase):
    """The real alerts/ tree must satisfy the local half of the contract."""

    def test_every_shipped_rule_carries_a_contract_shaped_runbook_url(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        paths = sorted(str(p) for p in (root / "alerts").glob("*.yaml"))
        self.assertTrue(paths, "no alert files found")
        violations, anchors = check_paths(paths)
        self.assertEqual([str(v) for v in violations], [])
        rule_count = sum(
            len(yaml.safe_load(pathlib.Path(p).read_text())["rules"]) for p in paths
        )
        # check_paths keys anchors by slug, so a slug collision between two rule names
        # would silently merge them and lose a link target. One anchor per rule, always.
        self.assertEqual(len(anchors), rule_count)


if __name__ == "__main__":
    unittest.main()
