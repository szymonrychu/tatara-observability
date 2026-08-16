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
    SECTION_REPO,
    derive_bucket_bounds,
    derive_metric_names,
    parse_allowlist_sections,
    parse_histogram_bounds,
    reconcile,
    reconcile_bounds,
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


class DeriveBucketBoundsTest(unittest.TestCase):
    """B2 of tatara-observability#111: scripts/histogram_bounds.txt is a
    hand-transcribed copy of a number that lives in another repo, so it is
    re-derived from that repo's own source on every run rather than trusted."""

    def _repo(self, tmp: pathlib.Path, content: str) -> pathlib.Path:
        repo_dir = tmp / "repo" / "internal" / "obs"
        repo_dir.mkdir(parents=True)
        (repo_dir / "metrics.go").write_text(content)
        return tmp / "repo"

    def _derive(self, content: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return derive_bucket_bounds(self._repo(pathlib.Path(tmp), content))

    def test_exponential_buckets(self):
        # start * factor^(count-1) = 0.05 * 2^9 = 25.6
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogramVec(prometheus.HistogramOpts{\n"
                '    Name:    "operator_turn_submit_duration_seconds",\n'
                "    Buckets: prometheus.ExponentialBuckets(0.05, 2, 10),\n"
                '}, []string{"kind"})\n'
            ),
            {"operator_turn_submit_duration_seconds": (0.05, 25.6)},
        )

    def test_linear_buckets(self):
        # start + width*(count-1) = 10 + 5*4 = 30
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "x_seconds",\n'
                "    Buckets: prometheus.LinearBuckets(10, 5, 5),\n"
                "})\n"
            ),
            {"x_seconds": (10.0, 30.0)},
        )

    def test_def_buckets(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogramVec(prometheus.HistogramOpts{\n"
                '    Name:    "lightrag_call_duration_seconds",\n'
                "    Buckets: prometheus.DefBuckets,\n"
                '}, []string{"op"})\n'
            ),
            {"lightrag_call_duration_seconds": (0.005, 10.0)},
        )

    def test_literal_slice_takes_the_max(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogramVec(prometheus.HistogramOpts{\n"
                '    Name:    "operator_tasks_minted_per_sweep",\n'
                "    Buckets: []float64{0, 1, 2, 3, 5, 8, 13, 21},\n"
                "})\n"
            ),
            {"operator_tasks_minted_per_sweep": (0.0, 21.0)},
        )

    def test_literal_slice_with_go_underscore_separators(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogramVec(prometheus.HistogramOpts{\n"
                '    Name: "operator_bundle_bytes",\n'
                "    Help: \"x\",\n"
                "    Buckets: []float64{4_000, 16_000, 800_000},\n"
                "})\n"
            ),
            {"operator_bundle_bytes": (4000.0, 800000.0)},
        )

    def test_named_variable_buckets_are_underivable_not_guessed(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "code_graph_analytics_duration_seconds",\n'
                "    Buckets: analyticsDurationBuckets,\n"
                "})\n"
            ),
            {},
        )

    def test_absent_buckets_field_is_underivable(self):
        # Prometheus defaults an omitted Buckets to DefBuckets, but proving the
        # field is absent (rather than just outside the scan window) is not
        # something a text scan can do safely. Do not guess.
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name: "no_buckets_seconds",\n'
                '    Help: "x",\n'
                "})\n"
            ),
            {},
        )

    def test_a_later_histograms_buckets_do_not_bleed_into_an_earlier_one(self):
        # Two adjacent declarations: the first omits Buckets. Its scan window must
        # stop at the next constructor rather than adopting the second's ladder.
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name: "first_seconds",\n'
                "})\n"
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "second_seconds",\n'
                "    Buckets: prometheus.DefBuckets,\n"
                "})\n"
            ),
            {"second_seconds": (0.005, 10.0)},
        )

    def test_append_to_def_buckets_is_underivable_not_wrong(self):
        # The most common way a Go service widens DefBuckets. Deriving 10 here
        # would be a WRONG bound, and a wrong bound is worse than an absent one:
        # a mismatch is a hard CI failure telling the author to commit 10.
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "widened_seconds",\n'
                "    Buckets: append(prometheus.DefBuckets, 30, 60, 300),\n"
                "})\n"
            ),
            {},
        )

    def test_append_around_linear_buckets_is_underivable(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "widened2_seconds",\n'
                "    Buckets: append(prometheus.LinearBuckets(1, 1, 10), 3600),\n"
                "})\n"
            ),
            {},
        )

    def test_a_ladder_quoted_in_a_help_string_is_not_the_buckets(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "helped_seconds",\n'
                '    Help:    "ladder is []float64{0.5, 1, 2} scaled by tier",\n'
                "    Buckets: tierBuckets,\n"
                "})\n"
            ),
            {},
        )

    def test_a_ladder_left_in_a_comment_is_not_the_buckets(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "commented_seconds",\n'
                "    // was Buckets: []float64{0.1, 0.25, 0.5} until we widened it\n"
                "    Buckets: cBuckets,\n"
                "})\n"
            ),
            {},
        )

    def test_a_ladder_in_a_block_comment_is_not_the_buckets(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name: "blockcommented_seconds",\n'
                "    /*\n"
                "    Buckets: prometheus.ExponentialBuckets(1, 2, 3)\n"
                "    */\n"
                "    Buckets: prometheus.DefBuckets,\n"
                "})\n"
            ),
            {"blockcommented_seconds": (0.005, 10.0)},
        )

    def test_a_ladder_in_a_raw_string_is_not_the_buckets(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name: "rawstring_seconds",\n'
                "    Help: `multi line help\n"
                "Buckets: prometheus.ExponentialBuckets(1, 2, 3)\n"
                "`,\n"
                "    Buckets: prometheus.DefBuckets,\n"
                "})\n"
            ),
            {"rawstring_seconds": (0.005, 10.0)},
        )

    def test_a_neighbouring_var_ladder_is_not_adopted(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "neighbour_seconds",\n'
                "    Buckets: dBuckets,\n"
                "})\n"
                "\n"
                "var eBuckets = []float64{60, 300, 900}\n"
            ),
            {},
        )

    def test_promauto_forms_are_derived(self):
        self.assertEqual(
            self._derive(
                "promauto.With(reg).NewHistogramVec(prometheus.HistogramOpts{\n"
                '    Name:    "promauto_seconds",\n'
                "    Buckets: prometheus.DefBuckets,\n"
                '}, []string{"op"})\n'
            ),
            {"promauto_seconds": (0.005, 10.0)},
        )

    def test_exponential_buckets_range_is_underivable(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "ranged_seconds",\n'
                "    Buckets: prometheus.ExponentialBucketsRange(0.1, 60, 12),\n"
                "})\n"
            ),
            {},
        )

    def test_counters_are_not_histograms(self):
        self.assertEqual(
            self._derive(
                "prometheus.NewCounterVec(prometheus.CounterOpts{\n"
                '    Name: "operator_turn_submit_total",\n'
                "})\n"
            ),
            {},
        )

    def test_ignores_test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "internal").mkdir()
            (root / "internal" / "metrics_test.go").write_text(
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "test_only_seconds",\n'
                "    Buckets: prometheus.DefBuckets,\n"
                "})\n"
            )
            self.assertEqual(derive_bucket_bounds(root), {})


class ParseHistogramBoundsTest(unittest.TestCase):
    def test_assigns_bounds_to_their_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "histogram_bounds.txt"
            path.write_text(
                "# a preamble comment\n"
                "\n"
                "# --- operator: tatara-operator/internal/obs ---\n"
                "# ExponentialBuckets(0.05, 2, 10) -> 25.6\n"
                "operator_turn_submit_duration_seconds 0.05 25.6\n"
                "operator_bundle_bytes 4000 800000\n"
                "\n"
                "# --- memory: tatara-memory ---\n"
                "lightrag_call_duration_seconds 0.005 10\n"
            )
            self.assertEqual(
                parse_histogram_bounds(path),
                {
                    "operator_turn_submit_duration_seconds": ("operator", 0.05, 25.6),
                    "operator_bundle_bytes": ("operator", 4000.0, 800000.0),
                    "lightrag_call_duration_seconds": ("memory", 0.005, 10.0),
                },
            )


class ReconcileBoundsTest(unittest.TestCase):
    def _repo(self, tmp: pathlib.Path, name: str, content: str) -> pathlib.Path:
        repo_dir = tmp / name / "internal" / "obs"
        repo_dir.mkdir(parents=True)
        (repo_dir / "metrics.go").write_text(content)
        return tmp / name

    _OPERATOR_SRC = (
        "prometheus.NewHistogramVec(prometheus.HistogramOpts{\n"
        '    Name:    "operator_turn_submit_duration_seconds",\n'
        "    Buckets: prometheus.ExponentialBuckets(0.05, 2, 10),\n"
        "})\n"
        "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
        '    Name:    "operator_turn_duration_seconds",\n'
        "    Buckets: prometheus.ExponentialBuckets(5, 2, 8),\n"
        "})\n"
    )

    def test_matching_bound_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp), "tatara-operator", self._OPERATOR_SRC)
            entries = {
                "operator_turn_submit_duration_seconds": ("operator", 0.05, 25.6),
                "operator_turn_duration_seconds": ("operator", 5.0, 640.0),
            }
            mismatched, missing, unvalidatable, ghost, skipped = reconcile_bounds(
                entries, {"tatara-operator": repo}
            )
            self.assertEqual(mismatched, {})
            self.assertEqual(missing, {})
            self.assertEqual(unvalidatable, set())
            self.assertEqual(ghost, set())
            self.assertEqual(skipped, set())

    def test_drifted_bound_is_a_hard_failure(self):
        # The pre-mortem-2 case: a producer widened its buckets and the file kept
        # the old ceiling, so the guard would reject a threshold that is now legal.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp), "tatara-operator", self._OPERATOR_SRC)
            entries = {"operator_turn_submit_duration_seconds": ("operator", 0.05, 12.8)}
            mismatched = reconcile_bounds(entries, {"tatara-operator": repo}).mismatched
            self.assertEqual(
                mismatched,
                {"tatara-operator": {
                    "operator_turn_submit_duration_seconds": ((0.05, 12.8), (0.05, 25.6))
                }},
            )

    def test_float_representation_noise_is_not_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp), "tatara-operator", self._OPERATOR_SRC)
            entries = {
                "operator_turn_submit_duration_seconds": ("operator", 0.05, 25.600000001)
            }
            mismatched = reconcile_bounds(entries, {"tatara-operator": repo}).mismatched
            self.assertEqual(mismatched, {})

    def test_derivable_but_absent_is_informational(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp), "tatara-operator", self._OPERATOR_SRC)
            entries = {"operator_turn_submit_duration_seconds": ("operator", 0.05, 25.6)}
            mismatched, missing = reconcile_bounds(
                entries, {"tatara-operator": repo}
            )[:2]
            self.assertEqual(mismatched, {})
            self.assertEqual(
                missing, {"tatara-operator": {"operator_turn_duration_seconds": (5.0, 640.0)}}
            )

    def test_underivable_entry_is_reported_never_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                pathlib.Path(tmp),
                "tatara-memory",
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "code_graph_analytics_duration_seconds",\n'
                "    Buckets: analyticsDurationBuckets,\n"
                "})\n",
            )
            entries = {"code_graph_analytics_duration_seconds": ("memory", 0.5, 600.0)}
            mismatched, missing, unvalidatable = reconcile_bounds(
                entries, {"tatara-memory": repo}
            )[:3]
            self.assertEqual(mismatched, {})
            self.assertEqual(missing, {})
            self.assertEqual(unvalidatable, {"code_graph_analytics_duration_seconds"})

    def test_a_family_the_producer_no_longer_declares_is_a_ghost(self):
        # The direction histogram_bounds.txt's header promises cannot go stale
        # unnoticed: the producer renamed or deleted the histogram, so the
        # committed bound describes a series that no longer exists. That is the
        # `stale` allowlist case one level down, and it is a hard failure - NOT
        # the named-var `unvalidatable` case, which is intentional and benign.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp), "tatara-operator", self._OPERATOR_SRC)
            entries = {"operator_turn_renamed_duration_seconds": ("operator", 0.05, 25.6)}
            r = reconcile_bounds(entries, {"tatara-operator": repo})
            self.assertEqual(r.ghost, {"operator_turn_renamed_duration_seconds"})
            self.assertEqual(r.unvalidatable, set())

    def test_a_named_var_family_the_producer_still_declares_is_not_a_ghost(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                pathlib.Path(tmp),
                "tatara-memory",
                "prometheus.NewHistogram(prometheus.HistogramOpts{\n"
                '    Name:    "code_graph_analytics_duration_seconds",\n'
                "    Buckets: analyticsDurationBuckets,\n"
                "})\n",
            )
            entries = {"code_graph_analytics_duration_seconds": ("memory", 0.5, 600.0)}
            r = reconcile_bounds(entries, {"tatara-memory": repo})
            self.assertEqual(r.ghost, set())
            self.assertEqual(r.unvalidatable, {"code_graph_analytics_duration_seconds"})

    def test_clone_failure_is_a_neutral_skip(self):
        entries = {"ccw_turn_duration_seconds": ("wrapper", 1.0, 2048.0)}
        mismatched, missing, unvalidatable, ghost, skipped = reconcile_bounds(entries, {})
        self.assertEqual(mismatched, {})
        self.assertEqual(missing, {})
        self.assertEqual(unvalidatable, set())
        self.assertEqual(ghost, set())
        self.assertEqual(skipped, {"tatara-claude-code-wrapper"})

    def test_exempt_section_is_never_diffed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp), "tatara-operator", "")
            entries = {"some_external_seconds": ("external", 0.1, 5.0)}
            self.assertEqual(
                tuple(reconcile_bounds(entries, {"tatara-operator": repo})),
                ({}, {}, set(), set(), set()),
            )


class CommittedHistogramBoundsFileTest(unittest.TestCase):
    def test_every_entry_routes_to_a_known_section(self):
        entries = parse_histogram_bounds(
            pathlib.Path(__file__).resolve().parent / "histogram_bounds.txt"
        )
        self.assertTrue(entries)
        for family, (section, low, high) in entries.items():
            self.assertIn(section, SECTION_REPO, f"{family} has unroutable section")
            self.assertLess(low, high, family)


if __name__ == "__main__":
    unittest.main()
