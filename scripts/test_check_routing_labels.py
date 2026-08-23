#!/usr/bin/env python3
"""Tests for check_routing_labels. Run: python3 -m unittest scripts.test_check_routing_labels
or, from the scripts/ dir: python3 -m unittest test_check_routing_labels.

Offline. Fixtures are inline YAML written to a tmpdir; the live corpus is read from
alerts/*.yaml but never mutated.

The fixtures deliberately include rule shapes the current corpus does NOT contain. A
suite drawn only from today's clean 130 rules proves nothing: every one of them already
satisfies the contract, so a checker that returned "clean" unconditionally would pass.
Each violation case below is a rule the repo could ship tomorrow."""

import pathlib
import tempfile
import unittest

import yaml

import check_routing_labels as check

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ALERTS_DIR = REPO_ROOT / "alerts"


def rule(name="A rule", labels=None, **extra):
    r = {"name": name}
    if labels is not None:
        r["labels"] = labels
    r.update(extra)
    return r


def labels(severity="warning", homelab="true", system="tatara", component="operator"):
    out = {}
    if homelab is not None:
        out["homelab"] = homelab
    if system is not None:
        out["system"] = system
    if component is not None:
        out["component"] = component
    if severity is not None:
        out["severity"] = severity
    return out


def messages(violations):
    return " | ".join(v.message for v in violations)


class SystemArmTest(unittest.TestCase):
    """warning|critical => system=tatara. Omitting it is the failure with teeth: the
    alert falls past every child route to the homelab node, emails, is muted all
    weekend, and never mints an incident Task."""

    def test_warning_without_system_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels("warning", system=None)))
        self.assertEqual(len(v), 1)
        self.assertIn("system", messages(v))

    def test_critical_without_system_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels("critical", system=None)))
        self.assertEqual(len(v), 1)
        self.assertIn("system", messages(v))

    def test_warning_with_a_wrong_system_value_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels("warning", system="Tatara")))
        self.assertEqual(len(v), 1)

    def test_clean_warning_rule_passes(self):
        self.assertEqual(check.check_rule("f.yaml", rule(labels=labels("warning"))), [])

    def test_clean_critical_rule_passes(self):
        self.assertEqual(
            check.check_rule("f.yaml", rule(labels=labels("critical"))), []
        )


class InfoArmTest(unittest.TestCase):
    """info => system ABSENT. This is the #457 regression: two trend rules shipped with
    system=tatara, and each fire would have minted an incident Task for a condition
    nobody should be paged on. Corrected by a human reading label sets."""

    def test_info_with_system_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels("info")))
        self.assertEqual(len(v), 1)
        self.assertIn("system", messages(v))

    def test_info_without_system_passes(self):
        self.assertEqual(
            check.check_rule("f.yaml", rule(labels=labels("info", system=None))), []
        )

    def test_info_with_an_empty_system_value_is_still_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels("info", system="")))
        self.assertEqual(len(v), 1)


class HomelabArmTest(unittest.TestCase):
    """Every severity needs homelab=true: it is the parent route, and without it the
    rule leaves the subtree entirely - no mute window, no grouping, and the deleted
    default_labels cannot rescue it."""

    def test_missing_homelab_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels("warning", homelab=None)))
        self.assertEqual(len(v), 1)
        self.assertIn("homelab", messages(v))

    def test_homelab_false_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels("warning", homelab="false")))
        self.assertEqual(len(v), 1)
        self.assertIn("homelab", messages(v))

    def test_missing_homelab_on_an_info_rule_is_a_violation(self):
        v = check.check_rule(
            "f.yaml", rule(labels=labels("info", homelab=None, system=None))
        )
        self.assertEqual(len(v), 1)


class SeverityArmTest(unittest.TestCase):
    """An unrecognised severity is a hard failure, not a pass. A fourth severity must
    not silently inherit whichever arm the else branch happened to have."""

    def test_unrecognised_severity_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels("page")))
        self.assertEqual(len(v), 1)
        self.assertIn("severity", messages(v))

    def test_missing_severity_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels=labels(severity=None)))
        self.assertEqual(len(v), 1)
        self.assertIn("severity", messages(v))

    def test_an_unrecognised_severity_does_not_suppress_the_homelab_check(self):
        v = check.check_rule("f.yaml", rule(labels=labels("page", homelab=None)))
        self.assertEqual(len(v), 2)
        self.assertIn("homelab", messages(v))
        self.assertIn("severity", messages(v))


class LabelsPresenceTest(unittest.TestCase):
    """main.tf's ternary falls back to default_labels only when a rule declares none.
    Nothing wires default_labels any more, so that branch renders NO labels at all -
    the rule would route to the root receiver. Assert the branch stays unreachable."""

    def test_missing_labels_is_a_violation(self):
        v = check.check_rule("f.yaml", rule())
        self.assertEqual(len(v), 1)
        self.assertIn("labels", messages(v))

    def test_empty_labels_is_a_violation(self):
        v = check.check_rule("f.yaml", rule(labels={}))
        self.assertEqual(len(v), 1)
        self.assertIn("labels", messages(v))

    def test_missing_labels_reports_once_not_once_per_arm(self):
        self.assertEqual(len(check.check_rule("f.yaml", rule())), 1)


class WaiverTest(unittest.TestCase):
    """The waiver exists so a legitimate exception is visible in the diff instead of
    being achieved by deleting the check. It waives the severity=>system arm ONLY."""

    def test_justification_waives_a_missing_system_on_a_warning_rule(self):
        r = rule(
            labels=labels("warning", system=None),
            tatara_routing_justification="deliberately email-only, see #999",
        )
        self.assertEqual(check.check_rule("f.yaml", r), [])

    def test_justification_waives_system_on_an_info_rule(self):
        r = rule(
            labels=labels("info"),
            tatara_routing_justification="info condition that must mint a Task, see #999",
        )
        self.assertEqual(check.check_rule("f.yaml", r), [])

    def test_justification_does_not_waive_a_missing_homelab(self):
        r = rule(
            labels=labels("warning", homelab=None),
            tatara_routing_justification="deliberately email-only, see #999",
        )
        v = check.check_rule("f.yaml", r)
        self.assertEqual(len(v), 1)
        self.assertIn("homelab", messages(v))

    def test_justification_does_not_waive_an_unrecognised_severity(self):
        r = rule(
            labels=labels("page"),
            tatara_routing_justification="deliberately email-only, see #999",
        )
        self.assertEqual(len(check.check_rule("f.yaml", r)), 1)

    def test_justification_does_not_waive_missing_labels(self):
        r = rule(tatara_routing_justification="deliberately email-only, see #999")
        self.assertEqual(len(check.check_rule("f.yaml", r)), 1)

    def test_empty_justification_is_not_a_waiver(self):
        r = rule(labels=labels("warning", system=None), tatara_routing_justification="")
        self.assertEqual(len(check.check_rule("f.yaml", r)), 1)

    def test_whitespace_only_justification_is_not_a_waiver(self):
        r = rule(
            labels=labels("warning", system=None), tatara_routing_justification="   "
        )
        self.assertEqual(len(check.check_rule("f.yaml", r)), 1)

    def test_non_string_justification_is_not_a_waiver(self):
        r = rule(
            labels=labels("warning", system=None), tatara_routing_justification=True
        )
        self.assertEqual(len(check.check_rule("f.yaml", r)), 1)

    def test_a_waived_rule_is_reported_so_the_override_is_visible_in_ci(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "g.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "rules": [
                            rule(
                                name="Waived",
                                labels=labels("warning", system=None),
                                tatara_routing_justification="see #999",
                            ),
                            rule(name="Clean", labels=labels("warning")),
                        ]
                    }
                )
            )
            violations, waived = check.check_paths([str(path)])
        self.assertEqual(violations, [])
        self.assertEqual([w.rule for w in waived], ["Waived"])
        self.assertIn("see #999", str(waived[0]))


class CheckPathsTest(unittest.TestCase):
    def test_reports_every_offending_rule_in_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "g.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "rules": [
                            rule(name="Dark", labels=labels("warning", system=None)),
                            rule(name="Spammy", labels=labels("info")),
                            rule(name="Clean", labels=labels("critical")),
                        ]
                    }
                )
            )
            violations, waived = check.check_paths([str(path)])
        self.assertEqual(sorted(v.rule for v in violations), ["Dark", "Spammy"])
        self.assertEqual(waived, [])

    def test_a_file_with_no_rules_key_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "g.yaml"
            path.write_text("interval_seconds: 60\n")
            self.assertEqual(check.check_paths([str(path)]), ([], []))

    def test_violation_str_names_the_file_and_the_rule(self):
        v = check.check_rule(
            "alerts/x.yaml", rule(name="Dark", labels=labels("warning", system=None))
        )[0]
        self.assertIn("alerts/x.yaml", str(v))
        self.assertIn("Dark", str(v))


class MainTest(unittest.TestCase):
    def test_exit_1_on_a_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "g.yaml"
            path.write_text(
                yaml.safe_dump({"rules": [rule(labels=labels("warning", system=None))]})
            )
            self.assertEqual(check.main(["check_routing_labels.py", str(path)]), 1)

    def test_exit_0_on_a_clean_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "g.yaml"
            path.write_text(yaml.safe_dump({"rules": [rule(labels=labels("warning"))]}))
            self.assertEqual(check.main(["check_routing_labels.py", str(path)]), 0)

    def test_exit_2_on_an_unreadable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(pathlib.Path(tmp) / "nope.yaml")
            self.assertEqual(check.main(["check_routing_labels.py", missing]), 2)


class LiveCorpusTest(unittest.TestCase):
    """The corpus is the regression test for pre-mortem 4: a checker that turns
    alerts/*.yaml green by 'harmonising' the info rules onto system=tatara has done
    the exact thing MEMORY.md forbids."""

    def setUp(self):
        self.paths = sorted(str(p) for p in ALERTS_DIR.glob("*.yaml"))
        self.assertTrue(self.paths)

    def test_the_live_corpus_satisfies_the_contract(self):
        violations, _ = check.check_paths(self.paths)
        self.assertEqual([str(v) for v in violations], [])

    def test_no_rule_in_the_live_corpus_needs_the_waiver(self):
        _, waived = check.check_paths(self.paths)
        self.assertEqual([w.rule for w in waived], [])

    def test_every_live_info_rule_omits_system(self):
        info = []
        for path in self.paths:
            data = yaml.safe_load(pathlib.Path(path).read_text()) or {}
            for r in data.get("rules") or []:
                lbl = r.get("labels") or {}
                if lbl.get("severity") == "info":
                    info.append((r["name"], lbl.get("system")))
        self.assertTrue(info, "no info rules left - the waiver arm is now untested")
        self.assertEqual([n for n, s in info if s is not None], [])

    def test_every_live_rule_declares_labels(self):
        undeclared = []
        for path in self.paths:
            data = yaml.safe_load(pathlib.Path(path).read_text()) or {}
            for r in data.get("rules") or []:
                if not (r.get("labels") or {}):
                    undeclared.append(r.get("name"))
        self.assertEqual(undeclared, [])


if __name__ == "__main__":
    unittest.main()
