#!/usr/bin/env python3
"""Reconcile this repo's PromQL against what the producer repos actually emit,
in all THREE dimensions a selector has: metric name, label name, label value.

    dimension     | derived from                                  | direction
    --------------|-----------------------------------------------|----------
    metric NAME   | the Prometheus constructor's Name field       | both
    label NAME    | the same constructor's []string{...} slice    | both
    label VALUE   | the CRD's +kubebuilder:validation:Enum=       | both

Nothing validated a label NAME until #100, and that is the dimension that broke:
five rules selected operator_task_terminal_total{stage=...} on a metric labelled
{kind,state,stateReason} since v2.0.0, and BOTH provenance guards reported OK.
The metric name was emitted, so the reverse-drift check below passed; `stage` was
a checked label name and `failed`/`parked` were members of the hand-maintained
snapshot in the old scripts/stage_values_allowlist.txt, so the forward check
passed too. A membership test against a stale SUPERSET is silent by construction,
which is why both label dimensions are now DERIVED from the clone this script
already takes rather than copied into this repo. The label VALUE sweep moved here
from check_metric_provenance.py for the same reason.

WHAT THIS STILL DOES NOT CHECK. A label name and a label value are mechanical. A
rule whose THRESHOLD or SUMMARY describes a mechanism that does not exist is not,
and is still caught only by a human reading it. Three green dimensions are three
green dimensions, not a correct alert.

check_metric_provenance.py only catches ONE direction: an alert or dashboard
panel that selects a metric not on the allowlist (see that file's docstring).
It says nothing about an allowlist entry that lags a producer repo which has
REMOVED the metric: the name stays on the allowlist forever, and because
every alert file sets default_no_data_state: "OK", any rule still selecting
it reports OK / renders empty forever - the exact silent-green failure class
one level up. This script closes that hole: it shallow-clones each producer
repo at main, re-derives its emitted Prometheus metric names, and flags any
allowlist entry that no clone still emits.

DERIVATION. A metric name is recognised by anchoring on the Prometheus
constructor call itself - prometheus.NewCounterVec(...), promauto.With(reg).
NewGauge(...), prometheus.NewDesc("name", ...), prometheus.MustNewConstMetric
(...), etc. - and reading the `Name:`/`name:` field or the constructor's
positional string literal from the following few lines. The allowlist
header's documented one-liner (a bare `(Name|name): *"..."` grep) is NOT
safe run repo-wide: it also matches unrelated Go struct literals (Kubernetes
Container/Volume/Port names and the like) and produces heavy false-positive
noise. Anchoring on the constructor call eliminates that (verified against
tatara-operator and tatara-memory: 0 false positives, vs. dozens from the
bare grep - see MEMORY.md).

The label set is the CLOSING ARGUMENT of that same call, so the label-NAME
derivation costs one more read of a call this parser already locates:

    }, []string{"kind", "state", "stateReason"}),

A *Vec constructor's last argument is its label slice; NewDesc's THIRD
positional argument is (the fourth is constLabels); a non-Vec Counter/Gauge/
Histogram declares the EMPTY set, which is a resolved fact and not an
unresolved one - a label matcher on a label-less metric matches nothing.
Where the slice is built from a variable rather than a literal, the metric
maps to None: reported loudly in the summary, never a failure, and excluded
from both label checks. Guessing the empty set there would turn every live
selector on that metric dark.

LABEL CHECKS APPLY ONLY TO METRICS WHOSE CONSTRUCTOR WAS LOCATED AND WHOSE
SLICE RESOLVED. That single rule is what removes the whole foreign-exporter
noise class without one allowlist entry: no clone declares
volume_manager_total_volumes or kube_pod_status_ready, so their label names
and values are never this script's business - a metric no tatara repo emits
is the stale-NAME check's problem above and nothing else's. Infra labels that
NO constructor can declare because the scrape config adds them (job,
namespace, pod, ...) are listed once in scripts/label_exemptions.txt.

DIRECTIONALITY. Only the reverse direction (allowlist names no repo still
emits) is a hard failure. The forward direction (a repo emits a name that
is not on the allowlist) is reported in the job summary as informational
only and never fails the job: a component legitimately emits far more
metrics than are ever alerted on or dashboarded (confirmed: tatara-operator
alone emits ~107 names derived, only ~90 are allowlisted - the rest simply
have no rule yet, which is normal, not drift). Hard-failing that direction
would make this job permanently red on healthy state, which is exactly the
cron-fatigue failure mode issue #57's own pre-mortem warns against.

CLONE FAILURE = NEUTRAL SKIP, NEVER A HARD FAIL. A transient GitHub outage
or DNS hiccup must not block an unrelated alert/dashboard PR, and must not
turn a nightly cron run red for reasons nobody can act on. A repo that fails
to clone is skipped, with a warning, and its allowlist sections are left
unchecked for that run.

EXEMPT SECTIONS. Some allowlist sections are not derivable from any of these
repos by design and must never be diffed:
  - "external" (kube-state-metrics / kubelet metrics - not emitted by any
    tatara component).
  - claude_code_api_error_total, folded into the external section: a
    deliberately forward-looking entry for a metric that will come from a
    future OTel collector deploy (see alerts/tatara-usage-gate.yaml, rule
    "... - PENDING OTel deployment"), never from these repos' Go source.

Run: python3 scripts/reconcile_metric_provenance.py
Exit 0 = clean (including "all repos skipped"), 1 = stale allowlist entr(y/ies),
undeclared label name, or dead closed-set label value, 2 = usage/parse error.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Iterator

import check_metric_provenance as check

# Producer repos the allowlist's non-exempt sections are sourced from. All
# public - no clone auth needed.
REPOS: dict[str, str] = {
    "tatara-operator": "https://github.com/szymonrychu/tatara-operator.git",
    "tatara-claude-code-wrapper": "https://github.com/szymonrychu/tatara-claude-code-wrapper.git",
    "tatara-memory": "https://github.com/szymonrychu/tatara-memory.git",
    "tatara-memory-repo-ingester": "https://github.com/szymonrychu/tatara-memory-repo-ingester.git",
}

# metrics_allowlist.txt section-header key (the first word of the header
# text, see section_key()) -> which repo emits it, or None if the section is
# exempt from reconciliation (see the EXEMPT SECTIONS docstring paragraph).
# "quality" and "usage-gate" are operator-emitted sections, not separate
# repos - verified against internal/obs/operator_metrics.go and
# internal/obs/accountusage_metrics.go.
SECTION_REPO: dict[str, str | None] = {
    "operator": "tatara-operator",
    "quality": "tatara-operator",
    "usage-gate": "tatara-operator",
    "wrapper": "tatara-claude-code-wrapper",
    "memory": "tatara-memory",
    "ingester": "tatara-memory-repo-ingester",
    "external": None,
}

# Anchors a Prometheus metric constructor call. Group 1 is the constructor name,
# which is what tells a *Vec (label slice as its last argument) apart from a plain
# Counter/Gauge (no labels at all) and from NewDesc (labels third-positional).
# `promauto\.\w+(?:\([^)]*\))?` covers both promauto spellings: the stored factory
# (promauto.factory.NewCounterVec) and the canonical inline one
# (promauto.With(reg).NewGaugeVec). Missing the latter is a false FAILURE, not a
# skip: the metric goes unseen by derive_metric_names, so its allowlist entry reads
# as stale and the reverse-drift check hard-fails on a healthy producer.
_CTOR = re.compile(
    r"(?:prometheus|promauto\.\w+(?:\([^)]*\))?)\.(New\w+|MustNewConstMetric)\("
)
# The constructor's Name/name field, e.g. `Name: "operator_task_stage"`.
_NAME_FIELD = re.compile(r'(?:Name|name):\s*"([a-z][a-z0-9_]+)"')
# The constructor's positional string literal, e.g. NewDesc("operator_pushed_runs", ...).
_POSITIONAL = re.compile(r'NewDesc\(\s*"([a-z][a-z0-9_]+)"')
# How many lines after a constructor call to look for its Name field - covers
# multi-line prometheus.XOpts{...} literals.
_WINDOW = 8

# Constructor shapes, for the LABEL half of the derivation.
_VEC_CTOR = re.compile(r"^New(?:Counter|Gauge|Histogram|Summary|Untyped)Vec$")
_PLAIN_CTOR = re.compile(r"^New(?:Counter|Gauge|Histogram|Summary|Untyped)(?:Func)?$")
# A literal []string{...} label slice.
_LABEL_SLICE = re.compile(r"^\[\]string\{(.*)\}$", re.S)
_STR_LIT = re.compile(r'"([^"]*)"')

_SECTION_HEADER = re.compile(r"^#\s*---\s*(.+)$")
_SECTION_KEY = re.compile(r"^([a-z][a-z-]*)")

# CRD closed sets. A `+kubebuilder:validation:Enum=` marker binds to the FIELD it
# precedes, so the derivation is keyed by (struct, field) and cannot be confused by
# an adjacent field that carries no marker of its own (TaskStatus.StateReason).
_TYPE_DECL = re.compile(r"^type\s+([A-Z]\w*)\s+struct\s*\{")
_ENUM_MARKER = re.compile(r"^//\s*\+kubebuilder:validation:Enum=(.+)$")
_FIELD_DECL = re.compile(r"^([A-Z]\w*)\s+\S")

# stateReason's vocabulary, which has NO CRD enum marker: it is the union of two
# named slices in internal/stage/stage.go, whose elements are `const Reason* = "..."`
# identifiers rather than literals.
_REASON_CONST = re.compile(r'^(Reason[A-Za-z0-9_]*)\s*=\s*"([^"]*)"')
_REASON_SLICE = re.compile(r"^var\s+(\w+)\s*=\s*\[\]string\{$")

# PromQL label name -> the (Go struct, field) whose enum marker IS its closed set.
#
# `stage` binds to the State enum rather than being exempted wholesale: it survived
# v2.0.0 as a label name and operator_stage_drift_total{stage} is passed
# task.Status.State verbatim (internal/controller/task_controller.go:439), so it is
# genuinely checkable. The two metrics where `stage` means something else are
# exempted by name in scripts/label_exemptions.txt.
#
# park_reason is the same CRD field under a snake_case label name
# (operator_task_parked_with_live_pod_repaired_total). Mapping a label name, not a
# metric, is why the exemption machinery has to be metric-aware: `kind` is the A.4
# origin kind on the Task family and an access class, a CR kind or a decline class
# elsewhere.
CLOSED_SET_FIELDS: dict[str, tuple[str, str]] = {
    "kind": ("TaskSpec", "Kind"),
    "state": ("TaskStatus", "State"),
    "stage": ("TaskStatus", "State"),
    "parkReason": ("TaskStatus", "ParkReason"),
    "park_reason": ("TaskStatus", "ParkReason"),
    "agent_kind": ("TaskStatus", "AgentKind"),
    "agentKind": ("TaskStatus", "AgentKind"),
}

# PromQL label name -> the internal/stage slices whose union is its closed set.
CLOSED_SET_REASON_VARS: dict[str, tuple[str, ...]] = {
    "stateReason": ("RejectReasons", "DoneReasons"),
}

# The repo that DEFINES those vocabularies. Coupling the value sweep to one
# producer's CRD is correct here: that CRD is where the closed sets live.
CLOSED_SET_REPO = "tatara-operator"


def _go_source_files(repo_dir: pathlib.Path) -> list[pathlib.Path]:
    return [
        p
        for p in sorted(repo_dir.rglob("*.go"))
        if not p.name.endswith("_test.go") and not p.name.startswith("zz_generated")
    ]


def _skip_string_or_comment(text: str, i: int) -> int | None:
    """End offset of the Go string literal or line comment starting at i, or None.

    The paren/brace scanners below must not count a bracket inside a Help string
    ("... percent (0..100) ...") or inside a trailing // comment.
    """
    c = text[i]
    if c in '"`':
        j = i + 1
        while j < len(text):
            if text[j] == "\\" and c == '"':
                j += 2
                continue
            if text[j] == c:
                return j + 1
            j += 1
        return len(text)
    if text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    return None


def _split_args(body: str) -> list[str]:
    """Top-level comma-separated arguments of a call body.

    A multi-line call ends its argument list with a trailing comma, which would
    otherwise yield a final empty argument and make the label slice look
    unresolved (tatara-memory internal/memory/reaper.go).
    """
    args: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(body):
        end = _skip_string_or_comment(body, i)
        if end is not None:
            i = end
            continue
        c = body[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(body[start:i])
            start = i + 1
        i += 1
    args.append(body[start:])
    out = [a.strip() for a in args]
    while out and not out[-1]:
        out.pop()
    return out


def _call_args(text: str, open_paren: int) -> list[str] | None:
    """Arguments of the call whose "(" sits at open_paren, or None if unbalanced."""
    depth = 0
    i = open_paren
    while i < len(text):
        end = _skip_string_or_comment(text, i)
        if end is not None:
            i = end
            continue
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return _split_args(text[open_paren + 1 : i])
        i += 1
    return None


def _ctor_sites(
    repo_dir: pathlib.Path,
) -> Iterator[tuple[pathlib.Path, int, str, str, list[str] | None]]:
    """(path, lineno, ctor, window, args) for every Prometheus constructor call.

    The single walk both derivations anchor on, so the metric NAME and the label
    NAME can never be read off different sets of call sites.

    `args` needs an absolute offset into the file while `window` needs a line
    index, so the two must agree on where a line starts. split("\\n") - not
    splitlines(), which also breaks on \\x0b, \\x0c and \\u2028 - keeps them exact;
    read_text() has already normalised CRLF away in universal-newline mode.
    """
    for path in _go_source_files(repo_dir):
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        lines = text.split("\n")
        offset = 0
        for i, line in enumerate(lines):
            for m in _CTOR.finditer(line):
                window = "\n".join(lines[i : i + _WINDOW])
                args = _call_args(text, offset + m.end() - 1)
                yield path, i + 1, m.group(1), window, args
            offset += len(line) + 1


def _ctor_name(window: str) -> str | None:
    m = _POSITIONAL.search(window) or _NAME_FIELD.search(window)
    return m.group(1) if m else None


def derive_metric_names(repo_dir: pathlib.Path) -> set[str]:
    """Every Prometheus metric name registered anywhere under repo_dir.

    Anchored on the constructor call (see module docstring) rather than a
    bare `Name: "..."` grep, so Kubernetes manifest struct literals
    (ContainerPort{Name: "http"} and the like) are never mistaken for a
    metric.
    """
    names: set[str] = set()
    for _path, _line, _ctor, window, _args in _ctor_sites(repo_dir):
        name = _ctor_name(window)
        if name:
            names.add(name)
    return names


def _declared_labels(ctor: str, args: list[str] | None) -> frozenset[str] | None:
    """The label set a constructor call declares, or None if it cannot be read.

    None is NOT the empty set: see the DERIVATION paragraph in the module
    docstring. A constructor that declares no labels of its own (NewConstMetric
    and friends take an already-built Desc) returns None too - the NewDesc call
    that built that Desc is its own site in this walk.
    """
    if args is None:
        return None
    if _PLAIN_CTOR.match(ctor):
        return frozenset()
    if _VEC_CTOR.match(ctor):
        return _parse_label_slice(args[-1]) if args else None
    if ctor == "NewDesc":
        return _parse_label_slice(args[2]) if len(args) >= 3 else None
    return None


def _parse_label_slice(arg: str) -> frozenset[str] | None:
    arg = arg.strip()
    if arg == "nil":
        return frozenset()
    m = _LABEL_SLICE.match(arg)
    if not m:
        return None  # built from a variable - unresolvable, and must not be guessed
    return frozenset(_STR_LIT.findall(m.group(1)))


def derive_metric_labels(repo_dir: pathlib.Path) -> dict[str, frozenset[str] | None]:
    """{metric name: declared label set}, None where the slice could not be read.

    A name declared at two sites takes the UNION of its label sets, but a single
    unresolved site poisons the whole entry to None: half a label set would make
    live selectors look dark, which is the failure this check exists to prevent.
    """
    out: dict[str, frozenset[str] | None] = {}
    for _path, _line, ctor, window, args in _ctor_sites(repo_dir):
        name = _ctor_name(window)
        if name is None:
            continue
        labels = _declared_labels(ctor, args)
        if labels is None and not _is_declaration_site(ctor):
            continue  # NewConstMetric etc: not a declaration, not a failure to read
        if name not in out:
            out[name] = labels
        elif out[name] is None or labels is None:
            out[name] = None
        else:
            out[name] = out[name] | labels
    return out


def _is_declaration_site(ctor: str) -> bool:
    return bool(_VEC_CTOR.match(ctor) or _PLAIN_CTOR.match(ctor) or ctor == "NewDesc")


def derive_crd_enums(repo_dir: pathlib.Path) -> dict[tuple[str, str], frozenset[str]]:
    """{(struct, field): enum values} for every +kubebuilder:validation:Enum= in api/."""
    out: dict[tuple[str, str], frozenset[str]] = {}
    api_dir = repo_dir / "api"
    if not api_dir.is_dir():
        return out
    for path in _go_source_files(api_dir):
        struct: str | None = None
        pending: frozenset[str] | None = None
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for raw in lines:
            m = _TYPE_DECL.match(raw)
            if m:
                struct, pending = m.group(1), None
                continue
            if raw == "}":  # a struct's closing brace is at column 0
                struct, pending = None, None
                continue
            if struct is None:
                continue
            line = raw.strip()
            m = _ENUM_MARKER.match(line)
            if m:
                pending = frozenset(
                    v.strip() for v in m.group(1).split(";") if v.strip()
                )
                continue
            if not line or line.startswith("//"):
                continue  # other markers and doc comments do not break the binding
            field = _FIELD_DECL.match(line)
            if field and pending is not None:
                out[(struct, field.group(1))] = pending
            pending = None
    return out


def derive_reason_sets(repo_dir: pathlib.Path) -> dict[str, frozenset[str]]:
    """{Go slice name: values} for the []string reason vocabularies in internal/stage.

    A slice with even one element this parser cannot resolve is OMITTED, not
    returned short: half a closed set fails CI on live values, which is worse than
    not checking that label at all.
    """
    path = repo_dir / "internal" / "stage" / "stage.go"
    if not path.is_file():
        return {}
    lines = path.read_text(errors="ignore").splitlines()
    consts = dict(
        m.groups()
        for m in (_REASON_CONST.match(line.strip()) for line in lines)
        if m is not None
    )
    out: dict[str, frozenset[str]] = {}
    i = 0
    while i < len(lines):
        head = _REASON_SLICE.match(lines[i].strip())
        if head is None:
            i += 1
            continue
        values: set[str] = set()
        resolved = True
        i += 1
        while i < len(lines) and lines[i].strip() != "}":
            element = lines[i].strip().rstrip(",")
            if element and not element.startswith("//"):
                if element.startswith('"') and element.endswith('"'):
                    values.add(element[1:-1])
                elif element in consts:
                    values.add(consts[element])
                else:
                    resolved = False
            i += 1
        if resolved and values:
            out[head.group(1)] = frozenset(values)
        i += 1
    return out


def derive_closed_sets(repo_dir: pathlib.Path) -> dict[str, frozenset[str]]:
    """{PromQL label name: closed set}, from the CRD enums plus the reason slices.

    A label whose source cannot be read is ABSENT rather than empty: the value
    sweep then says nothing about it, which is the neutral outcome.
    """
    enums = derive_crd_enums(repo_dir)
    reasons = derive_reason_sets(repo_dir)
    out: dict[str, frozenset[str]] = {}
    for label, field in CLOSED_SET_FIELDS.items():
        if field in enums:
            out[label] = enums[field]
    for label, var_names in CLOSED_SET_REASON_VARS.items():
        if all(name in reasons for name in var_names):
            out[label] = frozenset().union(*(reasons[name] for name in var_names))
    return out


def load_label_exemptions(path: str) -> dict[str, set[str]]:
    """Parse scripts/label_exemptions.txt into {section: {entries}}.

    Format: `## <section>` headers, then one entry per line. Sections are
    `infra-labels`, `label-name:exempt-metrics` and `<label>:exempt-metrics`.
    """
    out: dict[str, set[str]] = {}
    current: str | None = None
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            if line.startswith("## "):
                current = line[3:].strip()
                out.setdefault(current, set())
            continue
        if current is not None:
            out[current].add(line)
    return out


class LabelFinding:
    """One selector that names a label its metric does not declare, or a dead value."""

    def __init__(
        self,
        path: str,
        context: str,
        metric: str,
        label: str,
        declared: frozenset[str] = frozenset(),
        value: str | None = None,
        no_op: bool = False,
    ):
        self.path = path
        self.context = context
        self.metric = metric
        self.label = label
        self.declared = declared
        self.value = value
        self.no_op = no_op

    @property
    def fatal(self) -> bool:
        return not self.no_op

    def __str__(self) -> str:
        if self.value is not None:
            return (
                f'{self.path}: {self.context} selects {self.metric}{{{self.label}='
                f'"{self.value}"}}, which is not in the closed set derived for '
                f"{self.label} from the operator CRD / internal/stage. A query "
                f"filtering on a dead label value reports OK / renders empty forever, "
                f"same as a dead metric name. If {self.label} means something else on "
                f"this metric, exempt it under `## {self.label}:exempt-metrics` in "
                f"scripts/label_exemptions.txt with the reason."
            )
        declared = ", ".join(sorted(self.declared)) or "no labels at all"
        if self.no_op:
            return (
                f"{self.path}: {self.context} NEGATIVELY matches "
                f"{self.metric}{{{self.label}!=...}}, but {self.metric} declares "
                f"{declared}. A negative matcher on a label the metric does not carry "
                f"matches every series, so the rule is not dark - it just filters "
                f"nothing, and any exclusion its summary promises is not happening. "
                f"Informational: this is also the forward-compatible idiom for a "
                f"matcher written before the producer adds the label."
            )
        return (
            f"{self.path}: {self.context} selects {self.metric}{{{self.label}=...}}, "
            f"but {self.metric} declares {declared}. A selector on a label the metric "
            f"does not carry matches NOTHING: the alert reports OK forever "
            f'(default_no_data_state: "OK") and the panel renders empty forever. The '
            f"label set is derived from the emitting Prometheus constructor in the "
            f"producer clone, so repoint the selector - or, if this label is added by "
            f"scrape relabelling rather than by the constructor, list {self.metric} "
            f"under `## label-name:exempt-metrics` in scripts/label_exemptions.txt."
        )


def check_label_names(
    expressions: list[tuple[str, str, str]],
    metric_labels: dict[str, frozenset[str] | None],
    exemptions: dict[str, set[str]],
) -> list[LabelFinding]:
    """Selectors naming a label their emitting metric does not declare.

    Deduplicated per (path, context, metric, label): a ratio rule names the same
    metric twice in one expression and a panel can carry several targets on it, and
    a summary that lists one defect three times reads as three defects. Two rules
    with the same dark selector stay two findings - they are two rules to fix.
    """
    infra = exemptions.get("infra-labels", set())
    exempt = exemptions.get("label-name:exempt-metrics", set())
    out: list[LabelFinding] = []
    seen: set[tuple[str, str, str, str]] = set()
    for path, context, expr in expressions:
        for sel in check.selector_labels(expr):
            declared = metric_labels.get(sel.metric)
            if declared is None:
                continue  # no constructor located, or its slice did not resolve
            if sel.label in infra or sel.metric in exempt:
                continue
            if sel.label in declared:
                continue
            key = (path, context, sel.metric, sel.label)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                LabelFinding(
                    path,
                    context,
                    sel.metric,
                    sel.label,
                    declared=declared,
                    no_op=not sel.positive,
                )
            )
    return out


def check_label_values(
    expressions: list[tuple[str, str, str]],
    metric_labels: dict[str, frozenset[str] | None],
    closed_sets: dict[str, frozenset[str]],
    exemptions: dict[str, set[str]],
) -> list[LabelFinding]:
    """Selectors filtering on a value outside its label's derived closed set.

    A dead value in a NEGATIVE matcher is fatal too, and for the opposite reason:
    the rule means to exclude it, so a renamed vocabulary silently stops excluding
    and starts firing on exactly what the summary says it ignores.

    Deduplicated per (path, context, metric, label, value), same reasoning as
    check_label_names.
    """
    out: list[LabelFinding] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for path, context, expr in expressions:
        for sel in check.selector_labels(expr):
            declared = metric_labels.get(sel.metric)
            if declared is None or sel.label not in declared:
                continue  # an undeclared label is check_label_names' finding, not two
            allowed = closed_sets.get(sel.label)
            if allowed is None:
                continue  # no closed set derived for this label name
            if sel.metric in exemptions.get(f"{sel.label}:exempt-metrics", set()):
                continue  # the label name means something else on this metric
            for value in sorted(sel.values):
                if value in allowed:
                    continue
                key = (path, context, sel.metric, sel.label, value)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    LabelFinding(path, context, sel.metric, sel.label, value=value)
                )
    return out


def section_key(header_text: str) -> str | None:
    """The section key ("operator", "wrapper", ...) for a `# --- ... ---`
    header line's text, or None if it doesn't start with a recognised key.
    """
    m = _SECTION_KEY.match(header_text.strip().lower())
    return m.group(1) if m else None


def parse_allowlist_sections(path: pathlib.Path) -> dict[str, str]:
    """{metric_name: section_key} for every entry in metrics_allowlist.txt.

    A `# --- <text> ---` line starts a new section; its key is the leading
    word of <text> (before the first ":" or space), which stays stable even
    when the surrounding header prose changes. Multi-line header
    continuations (comment lines that don't start with "# ---") are
    comments, not new sections. Any other non-comment, non-blank line is an
    allowlist entry belonging to the current section.
    """
    out: dict[str, str] = {}
    current: str | None = None
    for line in path.read_text().splitlines():
        stripped = line.strip()
        header = _SECTION_HEADER.match(stripped)
        if header:
            current = section_key(header.group(1))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if current is not None:
            out[stripped] = current
    return out


def clone_repo(name: str, url: str, dest: pathlib.Path) -> bool:
    """Shallow-clone url@main into dest. Never raises: a clone failure is a
    neutral skip, not a hard CI failure (see module docstring)."""
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                "main",
                "--quiet",
                url,
                str(dest),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"::warning::reconcile_metric_provenance: could not clone {name} ({exc}) - "
            "skipping, treated as neutral (not a failure)",
            file=sys.stderr,
        )
        return False


def reconcile(
    entries: dict[str, str], repo_dirs: dict[str, pathlib.Path]
) -> tuple[dict[str, set[str]], dict[str, set[str]], set[str]]:
    """Diff each cloned repo's derived metric set against its allowlist
    sections. Returns (stale, new, skipped):
      stale[repo] = allowlisted names that repo no longer emits (hard-fail).
      new[repo]   = names that repo emits but no allowlist section carries
                    yet (informational only, see module docstring).
      skipped     = repo names whose clone failed and were therefore not
                    checked.
    """
    by_repo: dict[str, set[str]] = {}
    for metric, section in entries.items():
        repo = SECTION_REPO.get(section)
        if repo is None:
            continue  # exempt section - never diffed
        by_repo.setdefault(repo, set()).add(metric)

    stale: dict[str, set[str]] = {}
    new: dict[str, set[str]] = {}
    skipped: set[str] = set()
    for repo, allowlisted in by_repo.items():
        repo_dir = repo_dirs.get(repo)
        if repo_dir is None:
            skipped.add(repo)
            continue
        derived = derive_metric_names(repo_dir)
        repo_stale = allowlisted - derived
        repo_new = derived - allowlisted
        if repo_stale:
            stale[repo] = repo_stale
        if repo_new:
            new[repo] = repo_new
    return stale, new, skipped


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def value_sweep_coverage(
    metric_labels: dict[str, frozenset[str] | None],
    closed_sets: dict[str, frozenset[str]],
    exemptions: dict[str, set[str]],
) -> list[tuple[str, str]]:
    """Every (metric, label) pair whose VALUES this run will membership-test.

    That scope is (metrics declaring a closed-set label) MINUS (the exemptions), and
    it is a set nobody can read off either file. It has to be printed, because the
    value dimension is the one place where "default is to CHECK" cuts against this
    script's own thesis: a label name's vocabulary is a property of the (metric,
    label) pair, and nothing in the CRD says which metric's `kind` is TaskSpec.Kind
    and which is a CR kind or a webhook event kind. So label_exemptions.txt IS a
    hand-maintained binding - the one thing #100 set out to delete - and the only
    honest mitigation is that it fails LOUD (an unaudited overload turns CI red on
    correct work, it does not silently pass a dead value) and that its coverage is
    visible every night instead of inferred.

    The NAME dimension has no equivalent problem: whether a metric declares a label
    is metric-specific but fully derived, so default-CHECK there is free.
    """
    out: list[tuple[str, str]] = []
    for metric, labels in sorted(metric_labels.items()):
        if labels is None:
            continue
        if metric in exemptions.get("label-name:exempt-metrics", set()):
            continue
        for label in sorted(labels):
            if label not in closed_sets:
                continue
            if metric in exemptions.get(f"{label}:exempt-metrics", set()):
                continue
            out.append((metric, label))
    return out


def merge_metric_labels(
    per_repo: dict[str, dict[str, frozenset[str] | None]],
) -> dict[str, frozenset[str] | None]:
    """One {metric: declared labels} map across every clone that succeeded.

    A name declared in two repos takes the union of its label sets
    (http_requests_total is emitted by both tatara-memory and the ingester); a
    single unresolved declaration anywhere poisons the entry to None. No clones at
    all yields an empty map, which makes both label checks silent - that is the
    clone-failure contract, not an assertion that nothing declares any labels.
    """
    out: dict[str, frozenset[str] | None] = {}
    for labels_by_name in per_repo.values():
        for name, labels in labels_by_name.items():
            if name not in out:
                out[name] = labels
            elif out[name] is None or labels is None:
                out[name] = None
            else:
                out[name] = out[name] | labels
    return out


def _write_summary(
    stale: dict[str, set[str]],
    new: dict[str, set[str]],
    skipped: set[str],
    label_names: list[LabelFinding],
    label_values: list[LabelFinding],
    unresolved: list[str],
    closed_sets: dict[str, frozenset[str]],
    coverage: list[tuple[str, str]],
) -> None:
    import os

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["## Metric provenance reconciliation", ""]
    dark_names = [f for f in label_names if f.fatal]
    no_op_names = [f for f in label_names if not f.fatal]
    if dark_names:
        lines.append(
            "### DARK LABEL NAME - selector names a label its metric does not declare "
            "(must fix)"
        )
        lines.append("")
        lines += [f"- {f}" for f in dark_names]
        lines.append("")
    if label_values:
        lines.append(
            "### DEAD LABEL VALUE - value outside the closed set derived from the "
            "operator CRD (must fix)"
        )
        lines.append("")
        lines += [f"- {f}" for f in label_values]
        lines.append("")
    if stale:
        lines.append(
            "### STALE - allowlist entries no producer repo still emits (must fix)"
        )
        lines.append("")
        for repo, names in sorted(stale.items()):
            lines.append(f"**{repo}**")
            for name in sorted(names):
                lines.append(
                    f"- `{name}` - remove from `scripts/metrics_allowlist.txt` "
                    "(and repoint/delete any alert or dashboard panel still "
                    "selecting it - see check_metric_provenance.py)"
                )
        lines.append("")
    if new:
        lines.append("### New (informational only) - emitted, not yet allowlisted")
        lines.append("")
        for repo, names in sorted(new.items()):
            lines.append(f"**{repo}**: " + ", ".join(f"`{n}`" for n in sorted(names)))
        lines.append("")
    if no_op_names:
        lines.append(
            "### No-op filter (informational only) - NEGATIVE matcher on a label the "
            "metric does not declare"
        )
        lines.append("")
        lines += [f"- {f}" for f in no_op_names]
        lines.append("")
    if unresolved:
        # Loud, never fatal: this is the one shape the parser cannot read, and a
        # growing list here means the label-NAME check is quietly covering less.
        lines.append(
            "### Label slice UNRESOLVED (informational, never a failure) - these "
            "metrics build their `[]string{...}` from a variable, so neither label "
            "check applies to them: " + ", ".join(f"`{n}`" for n in unresolved)
        )
        lines.append("")
    if not closed_sets:
        lines.append(
            f"### Closed-set label VALUE sweep SKIPPED - no `{CLOSED_SET_REPO}` clone "
            "to derive the CRD enums from (neutral, not a failure)."
        )
        lines.append("")
    else:
        expected = set(CLOSED_SET_FIELDS) | set(CLOSED_SET_REASON_VARS)
        missing = sorted(expected - set(closed_sets))
        if missing:
            lines.append(
                "### Closed set NOT DERIVED for: "
                + ", ".join(f"`{label}`" for label in missing)
                + " - values on these labels were not checked this run."
            )
            lines.append("")
        # See value_sweep_coverage's docstring: this dimension's scope is the one
        # hand-maintained binding left in the repo, so it gets printed rather than
        # inferred. A pair here whose label means something else on that metric is an
        # unaudited overload - it will turn CI red on the first correct selector, so
        # exempt it before that happens.
        by_label: dict[str, list[str]] = {}
        for metric, label in coverage:
            by_label.setdefault(label, []).append(metric)
        lines.append(
            f"### Closed-set label VALUES being checked ({len(coverage)} "
            "metric/label pair(s), informational)"
        )
        lines.append("")
        for label in sorted(by_label):
            lines.append(
                f"- `{label}` ({len(closed_sets[label])} values) on: "
                + ", ".join(f"`{m}`" for m in sorted(by_label[label]))
            )
        lines.append("")
    if skipped:
        lines.append(
            "### Skipped (clone failed, neutral - not checked this run): "
            + ", ".join(sorted(skipped))
        )
        lines.append("")
    if not (stale or new or skipped or label_names or label_values or unresolved):
        lines.append(
            "OK: every reconcilable allowlist section matches its producer repo, and "
            "every alert/dashboard selector names a declared label with a live value."
        )
    text = "\n".join(lines) + "\n"
    print(text)
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(text)


def _repo_expressions() -> list[tuple[str, str, str]]:
    """Every Prometheus expression this repo ships, via the shared parser."""
    import glob

    paths = sorted(
        glob.glob(str(_root() / "alerts" / "*.yaml"))
        + glob.glob(str(_root() / "dashboards" / "*.json"))
    )
    return list(check.iter_expressions(paths))


def main(argv: list[str]) -> int:
    del argv
    allowlist_path = _root() / "scripts" / "metrics_allowlist.txt"
    try:
        entries = parse_allowlist_sections(allowlist_path)
        exemptions = load_label_exemptions(
            str(_root() / "scripts" / "label_exemptions.txt")
        )
        expressions = _repo_expressions()
    except (OSError, ValueError) as exc:
        print(f"reconcile_metric_provenance: {exc}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="metric-provenance-") as tmp:
        tmp_path = pathlib.Path(tmp)
        repo_dirs: dict[str, pathlib.Path] = {}
        for name, url in REPOS.items():
            dest = tmp_path / name
            if clone_repo(name, url, dest):
                repo_dirs[name] = dest

        stale, new, skipped = reconcile(entries, repo_dirs)
        metric_labels = merge_metric_labels(
            {name: derive_metric_labels(d) for name, d in repo_dirs.items()}
        )
        closed_sets = (
            derive_closed_sets(repo_dirs[CLOSED_SET_REPO])
            if CLOSED_SET_REPO in repo_dirs
            else {}
        )

    label_names = check_label_names(expressions, metric_labels, exemptions)
    label_values = check_label_values(
        expressions, metric_labels, closed_sets, exemptions
    )
    unresolved = sorted(n for n, v in metric_labels.items() if v is None)
    coverage = value_sweep_coverage(metric_labels, closed_sets, exemptions)

    _write_summary(
        stale,
        new,
        skipped,
        label_names,
        label_values,
        unresolved,
        closed_sets,
        coverage,
    )

    fatal = [f for f in label_names + label_values if f.fatal]
    if stale or fatal:
        parts = []
        if stale:
            parts.append(
                f"{sum(len(v) for v in stale.values())} stale allowlist entr(y/ies) "
                "no producer repo still emits"
            )
        if fatal:
            parts.append(
                f"{len(fatal)} selector(s) naming an undeclared label or a dead "
                "closed-set label value"
            )
        print(f"FAIL: {'; '.join(parts)}. See the job summary above.", file=sys.stderr)
        return 1

    print(
        f"OK: {len(metric_labels)} derived metric declarations across "
        f"{len(repo_dirs)} clone(s); no stale allowlist entries, no undeclared label "
        "names, no dead closed-set label values. A rule whose THRESHOLD or SUMMARY "
        "describes a mechanism that does not exist is still human-only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
