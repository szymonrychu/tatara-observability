#!/usr/bin/env python3
"""Tests for reconcile_metric_provenance. Run either
`python3 -m unittest discover scripts -p 'test_*.py'` (what CI runs) or, from the
scripts/ dir, `python3 -m unittest test_reconcile_metric_provenance`. NOT
`python3 -m unittest scripts.test_...`: the modules under test import each other
by bare name, so scripts/ has to be on sys.path, which the package form does not
do. That form has never worked; the docstring used to claim it did.

No network calls: every derivation is exercised against synthetic .go fixtures
written to a tempdir, never a real clone. The one exception is
ShippedLabelExemptionsTest, which reads the real scripts/label_exemptions.txt -
deliberately, because the fixtures prove the machinery and only that test proves
the DATA that CI actually loads."""

import pathlib
import tempfile
import unittest

from reconcile_metric_provenance import (
    check_label_names,
    merge_metric_labels,
    check_label_values,
    derive_closed_sets,
    derive_crd_enums,
    derive_metric_labels,
    derive_metric_names,
    derive_reason_sets,
    load_label_exemptions,
    value_sweep_coverage,
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


class DeriveMetricLabelsTest(unittest.TestCase):
    """Every constructor shape actually present in the four producer repos.

    A shape whose label slice cannot be resolved to string literals must map to
    None (reported loudly, never a failure) rather than to the empty set, which
    would silently assert "this metric declares no labels" and make every
    selector on it look dark.
    """

    def _derive(self, rel: str, content: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return derive_metric_labels(root)

    def test_single_line_label_slice_after_a_multi_line_opts_literal(self):
        # internal/obs/task_metrics.go:40 - the shape this whole check exists for.
        self.assertEqual(
            self._derive(
                "internal/obs/task_metrics.go",
                "taskTerminalTotal: prometheus.NewCounterVec(prometheus.CounterOpts{\n"
                '    Name: "operator_task_terminal_total",\n'
                '    Help: "x",\n'
                '}, []string{"kind", "state", "stateReason"}),\n',
            ),
            {"operator_task_terminal_total": frozenset({"kind", "state", "stateReason"})},
        )

    def test_multi_line_call_with_a_trailing_comma(self):
        # tatara-memory internal/memory/reaper.go:81 - the slice is its own
        # argument line and the argument list ends with a comma, so a naive
        # "last argument" split yields "" and the metric would read unresolved.
        self.assertEqual(
            self._derive(
                "internal/memory/reaper.go",
                "m := prometheus.NewCounterVec(\n"
                "    prometheus.CounterOpts{\n"
                '        Name: "tatara_memory_tombstone_total",\n'
                '        Help: "y",\n'
                "    },\n"
                '    []string{"op"},\n'
                ")\n",
            ),
            {"tatara_memory_tombstone_total": frozenset({"op"})},
        )

    def test_histogram_vec_declares_its_slice_after_a_buckets_field(self):
        self.assertEqual(
            self._derive(
                "internal/obs/sweep_metrics.go",
                "var TasksMintedPerSweep = prometheus.NewHistogramVec(prometheus.HistogramOpts{\n"
                '    Name:    "operator_tasks_minted_per_sweep",\n'
                '    Help:    "z",\n'
                "    Buckets: []float64{0, 1, 2, 3},\n"
                '}, []string{"project", "stage"})\n',
            ),
            {"operator_tasks_minted_per_sweep": frozenset({"project", "stage"})},
        )

    def test_non_vec_collector_declares_the_empty_label_set(self):
        # A plain Counter/Gauge has NO labels, and that is a RESOLVED fact, not an
        # unresolved one: any selector with a label matcher on it matches nothing
        # (ROADMAP notes exactly this for ccw_commit_oversized_blob_skipped_total).
        self.assertEqual(
            self._derive(
                "internal/obs/accountusage_metrics.go",
                "accountUsagePollHealth: prometheus.NewGauge(prometheus.GaugeOpts{\n"
                '    Name: "tatara_account_usage_poll_health",\n'
                '    Help: "1 when healthy.",\n'
                "}),\n",
            ),
            {"tatara_account_usage_poll_health": frozenset()},
        )

    def test_new_desc_with_a_nil_variable_label_argument(self):
        # internal/pushmetrics/receiver.go:241 - NewDesc's THIRD positional
        # argument is the variable-label slice, not the last one (the fourth is
        # constLabels).
        self.assertEqual(
            self._derive(
                "internal/pushmetrics/receiver.go",
                "ch <- prometheus.MustNewConstMetric(\n"
                '    prometheus.NewDesc("operator_pushed_runs", "help", nil, nil),\n'
                "    prometheus.GaugeValue, float64(active),\n"
                ")\n",
            ),
            {"operator_pushed_runs": frozenset()},
        )

    def test_new_desc_with_a_literal_variable_label_slice(self):
        self.assertEqual(
            self._derive(
                "internal/pushmetrics/receiver.go",
                'desc := prometheus.NewDesc("operator_forwarded_total", "help", []string{"run"}, nil)\n',
            ),
            {"operator_forwarded_total": frozenset({"run"})},
        )

    def test_label_slice_built_from_a_variable_is_unresolved_not_empty(self):
        self.assertEqual(
            self._derive(
                "internal/obs/dynamic.go",
                "var names = []string{\"a\"}\n"
                "var V = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
                '    Name: "operator_dynamic_total",\n'
                "}, names)\n",
            ),
            {"operator_dynamic_total": None},
        )

    def test_promauto_factory_is_parsed_like_prometheus(self):
        self.assertEqual(
            self._derive(
                "internal/obs/auto.go",
                "var C = promauto.factory.NewCounterVec(prometheus.CounterOpts{\n"
                '    Name: "operator_auto_total",\n'
                '}, []string{"result"})\n',
            ),
            {"operator_auto_total": frozenset({"result"})},
        )

    def test_promauto_with_registerer_is_parsed_like_prometheus(self):
        # `promauto.With(reg).NewGaugeVec(...)` is the canonical promauto spelling and
        # the module docstring claims it is handled. It must be, and not only for the
        # label set: a metric this parser fails to SEE is absent from
        # derive_metric_names, so its allowlist entry reads as stale and the
        # reverse-drift check hard-fails. Missing this shape is a false FAILURE, not a
        # skip. No producer uses promauto today, which is exactly why it needs a test.
        self.assertEqual(
            self._derive(
                "internal/obs/auto.go",
                "var G = promauto.With(reg).NewGaugeVec(prometheus.GaugeOpts{\n"
                '    Name: "operator_promauto_total",\n'
                '}, []string{"result"})\n',
            ),
            {"operator_promauto_total": frozenset({"result"})},
        )

    def test_promauto_with_registerer_is_found_by_the_name_derivation_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "internal" / "obs" / "auto.go"
            path.parent.mkdir(parents=True)
            path.write_text(
                "var C = promauto.With(reg).NewCounter(prometheus.CounterOpts{\n"
                '    Name: "operator_promauto_plain_total",\n'
                "})\n"
            )
            self.assertEqual(
                derive_metric_names(root), {"operator_promauto_plain_total"}
            )

    def test_crlf_line_endings_do_not_drift_the_call_offsets(self):
        # The label slice is located by ABSOLUTE character offset into the file, so
        # a two-byte line terminator could slide every lookup past line 1 and bind
        # the arguments of some EARLIER call - degrading the metric to "unresolved",
        # a quiet loss of coverage rather than a loud failure. Path.read_text() reads
        # in universal-newline mode and hands back "\n" only, which is what makes the
        # arithmetic safe; this test pins that dependency rather than assuming it.
        self.assertEqual(
            self._derive(
                "internal/obs/crlf.go",
                "package obs\r\n" * 40
                + "var pre = strings.Join(a, b)\r\n"
                + "var C = prometheus.NewCounterVec(prometheus.CounterOpts{\r\n"
                + '    Name: "operator_crlf_total",\r\n'
                + '}, []string{"result"})\r\n',
            ),
            {"operator_crlf_total": frozenset({"result"})},
        )

    def test_two_declarations_on_one_line_collapse_permissively(self):
        # DOCUMENTED BLIND SPOT, absent from all four producer repos (every metric
        # there uses a multi-line Opts literal). The name is read from a line
        # WINDOW, so two constructors on one physical line both resolve to the
        # first Name field and their label sets are unioned. That fails PERMISSIVE
        # - a superset never reports a live selector as dark - which is the correct
        # direction for a shape nothing emits. Asserted so the behaviour is a
        # decision on record rather than a surprise.
        self.assertEqual(
            self._derive(
                "internal/obs/oneline.go",
                'var A, B = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "op_a_total"},'
                ' []string{"a"}), prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "op_b"},'
                ' []string{"b"})\n',
            ),
            {"op_a_total": frozenset({"a", "b"})},
        )

    def test_a_paren_inside_a_help_string_does_not_break_the_balance(self):
        # "percent (0..100)" is real (accountusage_metrics.go); an UNBALANCED paren
        # in help text would otherwise run the scanner off the end of the file.
        self.assertEqual(
            self._derive(
                "internal/obs/help.go",
                "var G = prometheus.NewGaugeVec(prometheus.GaugeOpts{\n"
                '    Name: "operator_util_percent",\n'
                '    Help: "Utilization percent (0..100 - note the unbalanced paren",\n'
                '}, []string{"window"})\n',
            ),
            {"operator_util_percent": frozenset({"window"})},
        )

    def test_test_files_are_ignored(self):
        self.assertEqual(
            self._derive(
                "internal/obs/metrics_test.go",
                "prometheus.NewCounterVec(prometheus.CounterOpts{"
                'Name: "test_only_total"}, []string{"x"})\n',
            ),
            {},
        )

    def test_one_unresolved_site_poisons_a_name_declared_twice(self):
        # If a name resolves in one place and not in another, the label set is not
        # trustworthy: a union would understate it and make live selectors dark.
        self.assertEqual(
            self._derive(
                "internal/obs/twice.go",
                "var A = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
                '    Name: "operator_twice_total",\n'
                '}, []string{"a"})\n'
                "var B = prometheus.NewCounterVec(prometheus.CounterOpts{\n"
                '    Name: "operator_twice_total",\n'
                "}, dynamicNames)\n",
            ),
            {"operator_twice_total": None},
        )


_TASK_TYPES_GO = """\
package v1alpha1

type TaskSpec struct {
\t// Kind is the ORIGIN kind, immutable.
\t// +optional
\t// +kubebuilder:validation:Enum=brainstorm;incident;implement;refine;review;documentation;takeover
\tKind string `json:"kind,omitempty"`
}

type TaskStatus struct {
\t// +optional
\t// +kubebuilder:validation:Enum=new;refined;under-implementation;done;rejected
\tState string `json:"state,omitempty"`

\t// StateReason carries NO enum marker - its vocabulary lives in internal/stage.
\t// +optional
\tStateReason string `json:"stateReason,omitempty"`

\t// +optional
\t// +kubebuilder:validation:Enum=merge-blocked;head-moving
\tParkReason string `json:"parkReason,omitempty"`
}
"""

_STAGE_GO = """\
package stage

const (
\tReasonDeclined           = "declined"
\tReasonFalsePositive      = "false-positive"
\tReasonDocTimeout         = "doc-timeout"
\tReasonMRMergedExternally = "mr-merged-externally"
\tReasonMergeBlocked       = "merge-blocked"
)

var RejectReasons = []string{
\tReasonDeclined,
\tReasonFalsePositive,
}

var DoneReasons = []string{
\tReasonDocTimeout,
\tReasonMRMergedExternally,
}
"""


def _operator_clone(tmp: str) -> pathlib.Path:
    root = pathlib.Path(tmp)
    (root / "api" / "v1alpha1").mkdir(parents=True)
    (root / "api" / "v1alpha1" / "task_types.go").write_text(_TASK_TYPES_GO)
    (root / "internal" / "stage").mkdir(parents=True)
    (root / "internal" / "stage" / "stage.go").write_text(_STAGE_GO)
    return root


class DeriveCrdEnumsTest(unittest.TestCase):
    def test_markers_are_keyed_by_struct_and_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            enums = derive_crd_enums(_operator_clone(tmp))
        self.assertEqual(
            enums[("TaskSpec", "Kind")],
            frozenset(
                {
                    "brainstorm",
                    "incident",
                    "implement",
                    "refine",
                    "review",
                    "documentation",
                    "takeover",
                }
            ),
        )
        self.assertEqual(
            enums[("TaskStatus", "State")],
            frozenset({"new", "refined", "under-implementation", "done", "rejected"}),
        )
        self.assertEqual(
            enums[("TaskStatus", "ParkReason")],
            frozenset({"merge-blocked", "head-moving"}),
        )

    def test_a_field_with_no_marker_has_no_entry(self):
        # TaskStatus.StateReason deliberately carries no enum marker: the check
        # must not invent one from the marker on the field above it.
        with tempfile.TemporaryDirectory() as tmp:
            enums = derive_crd_enums(_operator_clone(tmp))
        self.assertNotIn(("TaskStatus", "StateReason"), enums)


class DeriveReasonSetsTest(unittest.TestCase):
    def test_named_slices_resolve_through_the_const_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            sets = derive_reason_sets(_operator_clone(tmp))
        self.assertEqual(sets["RejectReasons"], frozenset({"declined", "false-positive"}))
        self.assertEqual(
            sets["DoneReasons"], frozenset({"doc-timeout", "mr-merged-externally"})
        )

    def test_an_unresolvable_element_drops_the_whole_slice(self):
        # Half a closed set is worse than none: it would fail CI on live values.
        with tempfile.TemporaryDirectory() as tmp:
            root = _operator_clone(tmp)
            (root / "internal" / "stage" / "stage.go").write_text(
                _STAGE_GO.replace("\tReasonFalsePositive,\n", "\tsomeOtherPackage.X,\n")
            )
            sets = derive_reason_sets(root)
        self.assertNotIn("RejectReasons", sets)
        self.assertIn("DoneReasons", sets)


class DeriveClosedSetsTest(unittest.TestCase):
    def test_promql_label_names_bind_to_their_derivation_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            sets = derive_closed_sets(_operator_clone(tmp))
        # `stage` survived v2.0.0 as a label name carrying STATE values, so it
        # binds to the State enum rather than being exempted wholesale.
        self.assertEqual(sets["stage"], sets["state"])
        self.assertIn("under-implementation", sets["stage"])
        # snake_case variant of the same CRD field (operator_task_parked_with_
        # live_pod_repaired_total labels it park_reason).
        self.assertEqual(sets["park_reason"], sets["parkReason"])
        # stateReason has no CRD enum: RejectReasons + DoneReasons is its source.
        self.assertEqual(
            sets["stateReason"],
            frozenset(
                {"declined", "false-positive", "doc-timeout", "mr-merged-externally"}
            ),
        )

    def test_a_missing_source_yields_no_set_rather_than_an_empty_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "api").mkdir()
            sets = derive_closed_sets(root)
        self.assertEqual(sets, {})


_EXEMPTIONS = {
    "infra-labels": {"job", "namespace", "pod", "le", "quantile", "__name__"},
    "label-name:exempt-metrics": {"operator_relabelled_total"},
    "kind:exempt-metrics": {"operator_scm_writes_total"},
    "state:exempt-metrics": {"operator_queue_age_seconds"},
}

_LABELS = {
    "operator_task_terminal_total": frozenset({"kind", "state", "stateReason"}),
    "operator_scm_writes_total": frozenset({"kind", "verb", "result"}),
    "operator_queue_age_seconds": frozenset({"project", "class", "state"}),
    "operator_relabelled_total": frozenset({"project"}),
    "operator_dynamic_total": None,
    "tatara_memory_op_total": frozenset({"op", "result"}),
}

_CLOSED_SETS = {
    "kind": frozenset({"brainstorm", "implement"}),
    "state": frozenset({"done", "rejected"}),
    "stage": frozenset({"done", "rejected"}),
    "stateReason": frozenset({"declined", "doc-timeout"}),
}


def _expr(expr: str) -> list:
    return [("alerts/x.yaml", 'rule "r"', expr)]


class CheckLabelNamesTest(unittest.TestCase):
    def test_undeclared_label_name_is_a_fatal_finding(self):
        # The #100 class: the metric name is emitted and the value is a member of
        # the old closed set, but the LABEL the rule filters on no longer exists.
        found = check_label_names(
            _expr('sum(operator_task_terminal_total{stage="failed"})'),
            _LABELS,
            _EXEMPTIONS,
        )
        self.assertEqual(len(found), 1, [str(f) for f in found])
        self.assertTrue(found[0].fatal)
        self.assertIn("stage", str(found[0]))
        self.assertIn("kind, state, stateReason", str(found[0]))

    def test_declared_label_name_is_clean(self):
        self.assertEqual(
            check_label_names(
                _expr('sum(operator_task_terminal_total{state="done"})'),
                _LABELS,
                _EXEMPTIONS,
            ),
            [],
        )

    def test_infra_labels_are_never_checked(self):
        # job/namespace/pod come from the scrape config's relabelling, never from
        # a producer's constructor. Without this the check would report ~280
        # findings on a healthy rule set and be turned off within a week.
        self.assertEqual(
            check_label_names(
                _expr(
                    'sum(operator_task_terminal_total{job="tatara-operator",'
                    'namespace="tatara",pod="x"})'
                ),
                _LABELS,
                _EXEMPTIONS,
            ),
            [],
        )

    def test_client_library_labels_are_never_checked(self):
        # `le` on a histogram bucket, `quantile` on a summary, and the reserved
        # `__name__` are synthesised by the Prometheus client library and can never
        # appear in a []string{...} slice. `..._bucket{le="0.5"}` is completely
        # ordinary PromQL, and hard-failing it would be a false failure on correct
        # work - the fastest route to this check being downgraded to a warning.
        self.assertEqual(
            check_label_names(
                _expr(
                    'sum(operator_task_terminal_total_bucket{le="0.5"}) '
                    '+ sum(operator_task_terminal_total{quantile="0.99"}) '
                    '+ count({__name__="operator_task_terminal_total"})'
                ),
                _LABELS,
                _EXEMPTIONS,
            ),
            [],
        )

    def test_metric_with_no_located_constructor_is_skipped(self):
        # kube-state-metrics families and `up` are emitted by no tatara repo, so
        # no clone can declare their labels. That is the stale-NAME check's
        # territory, never this one's.
        self.assertEqual(
            check_label_names(
                _expr('max(kube_pod_status_ready{condition="false"})'),
                _LABELS,
                _EXEMPTIONS,
            ),
            [],
        )

    def test_metric_with_an_unresolved_label_slice_is_skipped(self):
        self.assertEqual(
            check_label_names(
                _expr('sum(operator_dynamic_total{whatever="x"})'), _LABELS, _EXEMPTIONS
            ),
            [],
        )

    def test_per_metric_exemption_covers_relabelled_label_names(self):
        self.assertEqual(
            check_label_names(
                _expr('sum(operator_relabelled_total{derived="x"})'),
                _LABELS,
                _EXEMPTIONS,
            ),
            [],
        )

    def test_negative_matcher_on_an_absent_label_is_reported_but_not_fatal(self):
        # `class!="maintenance"` written before the producer adds `class` matches
        # EVERY series, so the rule is not dark - it just filters nothing. ROADMAP
        # records that as a deliberate forward-compatible pattern, so hard-failing
        # it would break a blessed idiom. It is still worth saying out loud,
        # because the rule's summary promises an exclusion it is not making.
        found = check_label_names(
            _expr('sum(tatara_memory_op_total{class!="maintenance"})'),
            _LABELS,
            _EXEMPTIONS,
        )
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].fatal)
        self.assertIn("matches every series", str(found[0]))

    def test_histogram_suffix_resolves_to_the_base_metric(self):
        found = check_label_names(
            _expr(
                "histogram_quantile(0.95, rate("
                'operator_task_terminal_total_bucket{stage="failed"}[5m]))'
            ),
            _LABELS,
            _EXEMPTIONS,
        )
        self.assertEqual(len(found), 1)

    def test_one_context_reports_a_finding_once(self):
        # A ratio rule names the same metric twice in one expression, and a panel
        # can carry several targets on it. lint_dashboard already deduplicates for
        # this reason; a summary that lists the same defect three times reads as
        # three defects.
        found = check_label_names(
            _expr(
                'sum(operator_task_terminal_total{stage="failed"}) / '
                'sum(operator_task_terminal_total{stage="failed"})'
            ),
            _LABELS,
            _EXEMPTIONS,
        )
        self.assertEqual(len(found), 1, [str(f) for f in found])

    def test_the_same_defect_in_two_contexts_is_reported_twice(self):
        # Deduplication is per (path, context, metric, label) - two different rules
        # with the same dark selector are two rules to fix.
        found = check_label_names(
            [
                ("alerts/x.yaml", 'rule "a"', 'sum(operator_task_terminal_total{stage="x"})'),
                ("alerts/x.yaml", 'rule "b"', 'sum(operator_task_terminal_total{stage="x"})'),
            ],
            _LABELS,
            _EXEMPTIONS,
        )
        self.assertEqual(len(found), 2)


class CheckLabelValuesTest(unittest.TestCase):
    def _check(self, expr: str):
        return check_label_values(_expr(expr), _LABELS, _CLOSED_SETS, _EXEMPTIONS)

    def test_dead_closed_set_value_is_a_fatal_finding(self):
        found = self._check('sum(operator_task_terminal_total{state="failed"})')
        self.assertEqual(len(found), 1, [str(f) for f in found])
        self.assertTrue(found[0].fatal)
        self.assertIn('state="failed"', str(found[0]))

    def test_live_value_is_clean(self):
        self.assertEqual(self._check('sum(operator_task_terminal_total{state="done"})'), [])

    def test_alternation_is_split_into_candidate_values(self):
        found = self._check(
            'sum(operator_task_terminal_total{state=~"done|parked|rejected"})'
        )
        self.assertEqual([f.value for f in found], ["parked"])

    def test_one_context_reports_a_dead_value_once(self):
        self.assertEqual(
            len(
                self._check(
                    'sum(operator_task_terminal_total{state="failed"}) / '
                    'sum(operator_task_terminal_total{state="failed"})'
                )
            ),
            1,
        )

    def test_dead_value_in_a_negative_matcher_is_still_fatal(self):
        # The opposite direction of the dark-rule failure: the rule intends to
        # EXCLUDE these, and when the vocabulary is renamed it silently stops
        # excluding them and starts firing on exactly what it was meant to ignore.
        found = self._check(
            'sum(operator_task_terminal_total{stateReason!~"declined|pod-not-ready"})'
        )
        self.assertEqual([f.value for f in found], ["pod-not-ready"])

    def test_overloaded_label_is_exempt_per_metric(self):
        # operator_scm_writes_total{kind="write"} is an ACCESS CLASS. The same
        # value on a Task-family metric still fails.
        self.assertEqual(self._check('sum(operator_scm_writes_total{kind="write"})'), [])
        self.assertEqual(
            len(self._check('sum(operator_task_terminal_total{kind="write"})')), 1
        )
        # operator_queue_age_seconds{state="Queued"} is a QueuedEvent state.
        self.assertEqual(
            self._check('max(operator_queue_age_seconds{state="Queued"})'), []
        )

    def test_label_outside_the_closed_set_scope_is_not_checked(self):
        self.assertEqual(self._check('sum(tatara_memory_op_total{op="get"})'), [])

    def test_undeclared_label_is_left_to_the_name_check(self):
        # One selector must not produce two findings saying the same thing.
        self.assertEqual(
            self._check('sum(operator_task_terminal_total{stage="failed"})'), []
        )

    def test_grafana_template_variables_and_regex_patterns_are_skipped(self):
        self.assertEqual(self._check('sum(operator_task_terminal_total{state=~"$s"})'), [])
        self.assertEqual(
            self._check('sum(operator_task_terminal_total{state=~"under-.*"})'), []
        )

    def test_metric_with_no_located_constructor_is_skipped(self):
        # volume_manager_total_volumes{state="desired_state_of_world"} is a
        # kubelet metric: no clone declares it, so the value sweep never sees it
        # and needs no allowlist entry to stay quiet.
        self.assertEqual(
            self._check('max(volume_manager_total_volumes{state="actual"})'), []
        )


class MergeMetricLabelsTest(unittest.TestCase):
    def test_same_name_in_two_repos_takes_the_union(self):
        # http_requests_total is declared by both tatara-memory and the ingester.
        self.assertEqual(
            merge_metric_labels(
                {
                    "tatara-memory": {"http_requests_total": frozenset({"route"})},
                    "tatara-memory-repo-ingester": {
                        "http_requests_total": frozenset({"status"})
                    },
                }
            ),
            {"http_requests_total": frozenset({"route", "status"})},
        )

    def test_one_unresolved_declaration_poisons_the_merged_entry(self):
        self.assertEqual(
            merge_metric_labels(
                {
                    "a": {"m": frozenset({"x"})},
                    "b": {"m": None},
                }
            ),
            {"m": None},
        )

    def test_no_clones_yields_nothing_to_check(self):
        # Every clone failing is the neutral outcome: both label checks then see an
        # empty declaration map and say nothing at all, per the clone-failure
        # contract in the module docstring.
        merged = merge_metric_labels({})
        self.assertEqual(merged, {})
        self.assertEqual(
            check_label_names(
                _expr('sum(operator_task_terminal_total{stage="failed"})'),
                merged,
                _EXEMPTIONS,
            ),
            [],
        )


class ClosedSetUnavailableTest(unittest.TestCase):
    def test_value_sweep_is_a_no_op_when_no_closed_set_was_derived(self):
        # The operator clone is the only source of the CRD enums. If it fails to
        # clone, the value sweep must say nothing rather than fail everything.
        self.assertEqual(
            check_label_values(
                _expr('sum(operator_task_terminal_total{state="failed"})'),
                _LABELS,
                {},
                _EXEMPTIONS,
            ),
            [],
        )


class ValueSweepCoverageTest(unittest.TestCase):
    """The value sweep's blast radius has to be visible, not inferred.

    Its scope is (every metric declaring a closed-set label) MINUS (the exemptions),
    which is a set nobody can read off either file. Left invisible, an unaudited
    overload sits there until the first selector turns CI red on correct work - the
    review finding that produced this function.
    """

    def test_lists_every_metric_label_pair_in_scope(self):
        pairs = value_sweep_coverage(_LABELS, _CLOSED_SETS, _EXEMPTIONS)
        self.assertIn(("operator_task_terminal_total", "kind"), pairs)
        self.assertIn(("operator_task_terminal_total", "state"), pairs)
        self.assertIn(("operator_task_terminal_total", "stateReason"), pairs)

    def test_excludes_exempted_pairs_but_not_the_metric_s_other_labels(self):
        pairs = value_sweep_coverage(_LABELS, _CLOSED_SETS, _EXEMPTIONS)
        # operator_scm_writes_total is exempted for `kind` only...
        self.assertNotIn(("operator_scm_writes_total", "kind"), pairs)
        # ...and operator_queue_age_seconds for `state` only, so a future closed set
        # on one of its other labels would still be in scope.
        self.assertNotIn(("operator_queue_age_seconds", "state"), pairs)

    def test_excludes_labels_with_no_closed_set_and_unresolved_metrics(self):
        pairs = value_sweep_coverage(_LABELS, _CLOSED_SETS, _EXEMPTIONS)
        self.assertNotIn(("tatara_memory_op_total", "op"), pairs)
        self.assertEqual([p for p in pairs if p[0] == "operator_dynamic_total"], [])

    def test_is_empty_when_no_closed_set_was_derived(self):
        self.assertEqual(value_sweep_coverage(_LABELS, {}, _EXEMPTIONS), [])


class ShippedLabelExemptionsTest(unittest.TestCase):
    """The exemption fixtures above prove the machinery. This proves the DATA.

    Every test in this file could pass while the file CI actually loads is missing
    an entry, which is precisely the failure mode #100 is about.
    """

    def _shipped(self) -> dict:
        path = pathlib.Path(__file__).resolve().parent / "label_exemptions.txt"
        return load_label_exemptions(str(path))

    def test_infra_labels_cover_the_scrape_and_client_library_labels(self):
        infra = self._shipped()["infra-labels"]
        # Prometheus itself, and the kubernetes_sd relabelling:
        for label in ("job", "instance", "namespace", "pod", "container", "service"):
            self.assertIn(label, infra)
        # Synthesised by the client library, never present in a []string{...}:
        # `le` on every histogram bucket, `quantile` on every summary, and the
        # reserved metric-name label. `..._bucket{le="0.5"}` is ordinary PromQL.
        for label in ("le", "quantile", "__name__"):
            self.assertIn(label, infra)

    def test_every_audited_overload_stays_exempt(self):
        """The nine (metric, label) pairs whose vocabulary is provably NOT the CRD's.

        Each was traced to its WithLabelValues call site. Dropping one turns CI red on
        an ordinary selector, which is the failure mode that gets a guard switched off,
        so the audit is pinned here rather than living only in the file's comments.
        """
        sections = self._shipped()
        for label, metric in (
            # kind = the mirrored CR kind, Issue|MergeRequest
            ("kind", "operator_mirror_sync_total"),
            ("kind", "operator_mirror_comment_truncated_total"),
            # kind = the reaped resource kind, pod|service|task
            ("kind", "operator_reap_delete_error_total"),
            # kind = the inbound webhook event kind
            ("kind", "operator_webhook_events_total"),
            # kind = QueuedEventSpec.Kind, which carries no enum marker at all
            ("kind", "operator_queue_admitted_total"),
            # agent_kind = QueuedEventPayload.AgentKind: a DIFFERENT 7-value enum
            # that still contains `clarify`
            ("agent_kind", "operator_admission_wake_total"),
            # agent_kind = prompt.AgentKind(task), which falls back to Spec.Kind and
            # can therefore be `takeover`
            ("agent_kind", "operator_bundle_bytes"),
            ("agent_kind", "operator_bundle_elided_total"),
            # stage = the ingester's pipeline stage. CROSS-REPO: the first audit
            # looked only at tatara-operator/internal/obs and missed this.
            ("stage", "ingest_stage_duration_seconds"),
        ):
            self.assertIn(
                metric,
                sections.get(f"{label}:exempt-metrics", set()),
                f"{metric} must stay exempt from the {label} closed set",
            )

    def test_no_section_carries_a_vocabulary(self):
        # #100's root cause was this file holding a COPY of the operator's enums.
        # Every section here must be a set of metric names or of label names -
        # never a set of label values - so there is nothing left to rot.
        # Asserted by SHAPE, not as a fixed list of section names: a new
        # `<label>:exempt-metrics` section is ordinary maintenance, a section holding
        # label VALUES is the regression.
        for name, entries in self._shipped().items():
            if name == "infra-labels":
                continue
            self.assertRegex(
                name,
                r"^(?:label-name|[A-Za-z_][A-Za-z0-9_]*):exempt-metrics$",
                "every non-infra section must be a per-metric exemption",
            )
            for entry in entries:
                self.assertRegex(
                    entry,
                    r"^[a-z][a-z0-9_]*$",
                    f"{name} must list metric NAMES, got {entry!r}",
                )


class LoadLabelExemptionsTest(unittest.TestCase):
    def test_parses_sectioned_file(self):
        text = (
            "# header prose\n"
            "## infra-labels\n"
            "job\n"
            "namespace\n"
            "\n"
            "## kind:exempt-metrics\n"
            "# why this one is exempt\n"
            "operator_scm_writes_total\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "label_exemptions.txt"
            p.write_text(text)
            self.assertEqual(
                load_label_exemptions(str(p)),
                {
                    "infra-labels": {"job", "namespace"},
                    "kind:exempt-metrics": {"operator_scm_writes_total"},
                },
            )


if __name__ == "__main__":
    unittest.main()
