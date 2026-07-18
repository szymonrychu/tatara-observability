#!/usr/bin/env python3
"""Tests for reconcile_metric_provenance. Run:
python3 -m unittest scripts.test_reconcile_metric_provenance or, from the
scripts/ dir: python3 -m unittest test_reconcile_metric_provenance.

No network calls: derive_metric_names is exercised against synthetic .go
fixtures written to a tempdir, never a real clone."""

import pathlib
import tempfile
import unittest

from reconcile_metric_provenance import (
    derive_metric_names,
    parse_allowlist_sections,
    reconcile,
    section_key,
)


class DeriveMetricNamesTest(unittest.TestCase):
    def _write(self, tmp: pathlib.Path, rel: str, content: str) -> None:
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_finds_name_field_on_a_counter_vec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(
                root,
                "internal/obs/task_metrics.go",
                "var TaskTerminal = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
                '    Name: "operator_task_terminal_total",\n'
                '    Help: "x",\n'
                '}, []string{"kind"})\n',
            )
            self.assertEqual(
                derive_metric_names(root), {"operator_task_terminal_total"}
            )

    def test_finds_positional_new_desc(self):
        # The dynamic-name blind spot (NewDesc(name, ...) with a variable, not
        # a literal) is covered transitively by the producer's own static
        # declaration elsewhere - this test covers the literal form actually
        # used at tatara-operator internal/pushmetrics/receiver.go:241.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(
                root,
                "internal/pushmetrics/receiver.go",
                "ch <- prometheus.MustNewConstMetric(\n"
                '    prometheus.NewDesc("operator_pushed_runs", "help", nil, nil),\n'
                "    prometheus.GaugeValue, float64(active),\n"
                ")\n",
            )
            self.assertEqual(derive_metric_names(root), {"operator_pushed_runs"})

    def test_ignores_kubernetes_manifest_name_fields(self):
        # The false-positive class this anchoring exists to avoid: a bare
        # `Name: "..."` grep matches corev1.ContainerPort{Name: "http"} and
        # similar k8s builder literals that have nothing to do with metrics.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(
                root,
                "internal/memory/lightrag.go",
                "Ports: []corev1.ContainerPort{\n"
                '    {Name: "http", ContainerPort: 9621},\n'
                "},\n"
                'VolumeMounts: []corev1.VolumeMount{{Name: "data", MountPath: "/app/data"}},\n',
            )
            self.assertEqual(derive_metric_names(root), set())

    def test_ignores_test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(
                root,
                "internal/obs/task_metrics_test.go",
                'prometheus.NewCounterVec(prometheus.CounterOpts{Name: "test_only_total"}, nil)\n',
            )
            self.assertEqual(derive_metric_names(root), set())

    def test_finds_metrics_scattered_across_unrelated_packages(self):
        # tatara-memory has no metric declarations at all in internal/obs -
        # its metrics live in internal/lightrag, internal/ingest,
        # internal/codegraph, internal/memory/{service,reaper}.go and
        # internal/httpapi/middleware.go. No per-repo directory scoping, so
        # this must still find them wherever they live.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(
                root,
                "internal/lightrag/metrics.go",
                'prometheus.NewCounterVec(prometheus.CounterOpts{Name: "lightrag_calls_total"}, nil)\n',
            )
            self._write(
                root,
                "internal/httpapi/middleware.go",
                'prometheus.NewCounterVec(prometheus.CounterOpts{Name: "http_requests_total"}, nil)\n',
            )
            self.assertEqual(
                derive_metric_names(root),
                {"lightrag_calls_total", "http_requests_total"},
            )


class SectionKeyTest(unittest.TestCase):
    def test_extracts_key_before_colon(self):
        self.assertEqual(
            section_key("operator: new (CROSS-REPO-CONTRACT K.1)"), "operator"
        )

    def test_extracts_key_with_no_colon(self):
        self.assertEqual(
            section_key("wrapper (pushed through the operator receiver)"),
            "wrapper",
        )

    def test_hyphenated_key(self):
        self.assertEqual(
            section_key("usage-gate: unchanged by this redesign"), "usage-gate"
        )


class ParseAllowlistSectionsTest(unittest.TestCase):
    def test_assigns_entries_to_their_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "allowlist.txt"
            path.write_text(
                "# --- external: kubernetes / prometheus ---\n"
                "up\n"
                "\n"
                "# --- operator: new (CROSS-REPO-CONTRACT K.1) ---\n"
                "operator_task_stage\n"
                "operator_task_stage_age_seconds\n"
                "\n"
                "# --- wrapper (pushed through the operator receiver) ---\n"
                "ccw_turns_total\n"
            )
            self.assertEqual(
                parse_allowlist_sections(path),
                {
                    "up": "external",
                    "operator_task_stage": "operator",
                    "operator_task_stage_age_seconds": "operator",
                    "ccw_turns_total": "wrapper",
                },
            )

    def test_multiline_header_continuation_stays_in_the_same_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "allowlist.txt"
            path.write_text(
                "# --- operator: new, v6 addendum (present in contract K.1 but\n"
                "# missing from the original census) ---\n"
                "operator_doc_task_abandoned_total\n"
            )
            self.assertEqual(
                parse_allowlist_sections(path),
                {"operator_doc_task_abandoned_total": "operator"},
            )

    def test_comment_only_lines_are_not_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "allowlist.txt"
            path.write_text(
                "# --- memory: unchanged by this redesign ---\n"
                "# (operator_memory_stacks is listed in the operator section above)\n"
                "http_requests_total\n"
            )
            self.assertEqual(
                parse_allowlist_sections(path), {"http_requests_total": "memory"}
            )


class ReconcileTest(unittest.TestCase):
    def _repo(self, tmp: pathlib.Path, name: str, content: str) -> pathlib.Path:
        repo_dir = tmp / name
        (repo_dir / "internal" / "obs").mkdir(parents=True)
        (repo_dir / "internal" / "obs" / "metrics.go").write_text(content)
        return repo_dir

    def test_stale_allowlist_entry_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            repo = self._repo(
                tmp_path,
                "tatara-operator",
                'prometheus.NewCounterVec(prometheus.CounterOpts{Name: "operator_task_stage"}, nil)\n',
            )
            entries = {
                "operator_task_stage": "operator",
                "operator_brainstorm_outcome_total": "operator",
            }
            stale, new, skipped = reconcile(entries, {"tatara-operator": repo})
            self.assertEqual(
                stale, {"tatara-operator": {"operator_brainstorm_outcome_total"}}
            )
            self.assertEqual(new, {})
            self.assertEqual(skipped, set())

    def test_new_emitted_metric_is_reported_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            repo = self._repo(
                tmp_path,
                "tatara-operator",
                'prometheus.NewCounterVec(prometheus.CounterOpts{Name: "operator_task_stage"}, nil)\n'
                'prometheus.NewCounterVec(prometheus.CounterOpts{Name: "operator_unlisted_total"}, nil)\n',
            )
            entries = {"operator_task_stage": "operator"}
            stale, new, skipped = reconcile(entries, {"tatara-operator": repo})
            self.assertEqual(stale, {})
            self.assertEqual(new, {"tatara-operator": {"operator_unlisted_total"}})
            self.assertEqual(skipped, set())

    def test_exempt_sections_are_never_diffed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            repo = self._repo(tmp_path, "tatara-operator", "")
            entries = {
                "up": "external",
                "claude_code_api_error_total": "external",
            }
            stale, new, skipped = reconcile(entries, {"tatara-operator": repo})
            self.assertEqual(stale, {})
            self.assertEqual(new, {})
            self.assertEqual(skipped, set())

    def test_quality_and_usage_gate_sections_map_to_the_operator_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            repo = self._repo(
                tmp_path,
                "tatara-operator",
                'prometheus.NewCounterVec(prometheus.CounterOpts{Name: "operator_review_findings_total"}, nil)\n',
            )
            entries = {
                "operator_review_findings_total": "quality",
                "tatara_account_usage_poll_health": "usage-gate",
            }
            stale, new, skipped = reconcile(entries, {"tatara-operator": repo})
            self.assertEqual(
                stale, {"tatara-operator": {"tatara_account_usage_poll_health"}}
            )

    def test_unclonable_repo_is_skipped_not_failed(self):
        entries = {"ccw_turns_total": "wrapper"}
        stale, new, skipped = reconcile(entries, {})
        self.assertEqual(stale, {})
        self.assertEqual(new, {})
        self.assertEqual(skipped, {"tatara-claude-code-wrapper"})


if __name__ == "__main__":
    unittest.main()
