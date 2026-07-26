#!/usr/bin/env python3
"""Tests for check_alert_schema. Run: python3 -m unittest scripts.test_check_alert_schema
or, from the scripts/ dir: python3 -m unittest test_check_alert_schema."""

import pathlib
import tempfile
import unittest

import check_alert_schema as check

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ALERTS_DIR = REPO_ROOT / "alerts"
VARIABLES_TF = REPO_ROOT / "modules" / "grafana_alert" / "variables.tf"

# A cut-down stand-in for the module's type expression, with the same shape (nested
# object({...}) for rules and queries, optional()/comment noise in between) so the parser
# is tested on structure rather than on the exact live attribute list.
FAKE_VARIABLES_TF = """
variable "unrelated" {
  type = string
}

variable "alerts" {
  description = "List of JSON strings"
  nullable    = false
  type = list(object({
    rulegroup_name = string
    folder_uid     = string
    # a comment mentioning object({ that must not confuse the reader
    interval_seconds = optional(number, 60)
    default_labels   = optional(map(string), {}),
    rules = list(object({
      name = string,
      queries = list(object({
        expression     = optional(string, ""),
        datasource_uid = optional(string),
      })),
      threshold       = number
      for             = optional(string, "1m"),
      keep_firing_for = optional(string, "0"),
      annotations     = optional(map(string), {}),
    }))
  }))
}
"""


def _write(tmp: pathlib.Path, name: str, body: str) -> str:
    p = tmp / name
    p.write_text(body)
    return str(p)


class ParseAlertSchema(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parses_nested_levels(self):
        schema = check.parse_alert_schema(_write(self.tmp, "variables.tf", FAKE_VARIABLES_TF))
        self.assertIn("rulegroup_name", schema)
        self.assertIn("interval_seconds", schema)
        self.assertIn("keep_firing_for", schema["rules"])
        self.assertIn("expression", schema["rules"]["queries"])

    def test_map_and_scalar_attributes_have_no_nested_schema(self):
        schema = check.parse_alert_schema(_write(self.tmp, "variables.tf", FAKE_VARIABLES_TF))
        self.assertIsNone(schema["rules"]["annotations"])
        self.assertIsNone(schema["rules"]["threshold"])

    def test_missing_variable_block_raises(self):
        path = _write(self.tmp, "variables.tf", 'variable "other" {\n  type = string\n}\n')
        with self.assertRaises(ValueError):
            check.parse_alert_schema(path)

    def test_unrecognisable_shape_raises_rather_than_passing_everything(self):
        path = _write(
            self.tmp,
            "variables.tf",
            'variable "alerts" {\n  type = list(object({\n    something_else = string\n  }))\n}\n',
        )
        with self.assertRaises(ValueError):
            check.parse_alert_schema(path)

    def test_live_module_parses(self):
        schema = check.parse_alert_schema(str(VARIABLES_TF))
        self.assertIn("rules", schema)
        self.assertIn("keep_firing_for", schema["rules"])
        self.assertIn("for", schema["rules"])


class CheckKeys(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.schema = check.parse_alert_schema(
            _write(self.tmp, "variables.tf", FAKE_VARIABLES_TF)
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self, body: str):
        return check.check_file(_write(self.tmp, "rules.yaml", body), self.schema)

    def test_declared_keys_pass(self):
        body = """
interval_seconds: 60
rules:
  - name: "declared only"
    queries:
      - expression: "up"
    threshold: 0
    for: 10m
    keep_firing_for: 30m
    annotations:
      summary: "..."
"""
        self.assertEqual(self._violations(body), [])

    def test_undeclared_rule_key_is_a_violation(self):
        body = """
rules:
  - name: "typo'd knob"
    queries:
      - expression: "up"
    threshold: 0
    keep_firing_four: 30m
"""
        violations = self._violations(body)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].key, "keep_firing_four")
        self.assertEqual(violations[0].level, "rule")
        self.assertIn("silently", str(violations[0]))

    def test_undeclared_group_key_is_a_violation(self):
        body = """
default_no_data_state: "OK"
rules:
  - name: "fine"
    queries:
      - expression: "up"
    threshold: 0
"""
        violations = self._violations(body)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].key, "default_no_data_state")
        self.assertEqual(violations[0].level, "group")

    def test_undeclared_query_key_is_a_violation(self):
        body = """
rules:
  - name: "bad query key"
    queries:
      - expression: "up"
        relative_time_range_from: 1200
    threshold: 0
"""
        violations = self._violations(body)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].key, "relative_time_range_from")
        self.assertEqual(violations[0].level, "query")

    def test_annotation_and_label_keys_are_data_not_schema(self):
        body = """
rules:
  - name: "free-form maps"
    queries:
      - expression: "up"
    threshold: 0
    annotations:
      tatara_probe_exclusion: "producer-excluded"
      whatever_key: "..."
"""
        self.assertEqual(self._violations(body), [])

    def test_lint_only_top_level_key_is_exempt(self):
        body = """
tatara_exec_err_justification: "a query failure IS the condition here"
rules:
  - name: "fine"
    queries:
      - expression: "up"
    threshold: 0
"""
        self.assertEqual(self._violations(body), [])

    def test_lint_only_key_is_not_exempt_at_rule_level(self):
        body = """
rules:
  - name: "wrong scope"
    queries:
      - expression: "up"
    threshold: 0
    tatara_exec_err_justification: "belongs in annotations"
"""
        violations = self._violations(body)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].level, "rule")

    def test_every_undeclared_key_is_reported_not_just_the_first(self):
        body = """
rules:
  - name: "two bad keys"
    queries:
      - expression: "up"
    threshold: 0
    keep_firing_four: 30m
    missing_series_evals_to_resolve: 2
"""
        self.assertEqual(len(self._violations(body)), 2)

    def test_empty_file_is_clean(self):
        self.assertEqual(self._violations("\n"), [])


class RealAlertFilesPass(unittest.TestCase):
    def test_all_committed_alert_files_use_declared_keys_only(self):
        schema = check.parse_alert_schema(str(VARIABLES_TF))
        paths = sorted(str(p) for p in ALERTS_DIR.glob("*.yaml"))
        self.assertTrue(paths, "expected alerts/*.yaml to exist")
        violations = check.check_paths(paths, schema)
        self.assertEqual(
            violations,
            [],
            "committed alert files must only use declared keys:\n"
            + "\n".join(str(v) for v in violations),
        )

    def test_keep_firing_for_is_actually_threaded_into_the_resource(self):
        """Declaring an attribute is only half the fix - main.tf must render it too.

        A variables.tf-only declaration is the same silent no-op one layer down.
        """
        main_tf = (REPO_ROOT / "modules" / "grafana_alert" / "main.tf").read_text()
        self.assertRegex(main_tf, r"keep_firing_for\s*=\s*rule\.value\.keep_firing_for")


if __name__ == "__main__":
    unittest.main()
