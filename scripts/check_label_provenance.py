#!/usr/bin/env python3
"""Fail CI when an alert rule or a dashboard panel names a label the producing
metric does not carry - the label-NAME dimension issue #100 closes.

A PromQL selector has three dimensions and this repo guarded two of them:

  metric NAME  -> check_metric_provenance.py (alerts/dashboards -> allowlist)
                  and reconcile_metric_provenance.py (allowlist -> producer Go)
  label VALUE  -> check_metric_provenance.py, closed sets in
                  scripts/stage_values_allowlist.txt
  label NAME   -> nothing, until this script

That gap is not theoretical. tatara-operator v2.0.0 renamed the label
stage -> state on operator_task_terminal_total while KEEPING the metric name,
so 5 rules selected `operator_task_terminal_total{stage="failed"}` - a series
that cannot exist - and every check was green: the name check passed because
the metric is still emitted, and the VALUE check passed because `stage` was one
of the four label names it happened to know and `failed` was still a member of
a stale closed set. The trap is that the value sweep's own label names were
hard-coded, so `stage` being validated as a value made it look like the name
was checked too. It never was. A selector on a label the metric does not carry
matches NOTHING, forever, and with default_no_data_state: "OK" the rule reports
OK forever - the same silent-green class as a dead metric name, one dimension
over. A negative selector (`label!~"..."`) on a missing label is the mirror
image: it matches EVERYTHING, so the rule goes falsely NOISY instead of dark.
Both are caught here.

DERIVATION - FROM THE PRODUCER, NOT FROM A SNAPSHOT. The label set is read out
of the producer repos' Go source, from the same shallow clones and the same
constructor anchor reconcile_metric_provenance.py already uses. A Prometheus
metric declares its variable labels as the closing argument of the constructor
call that names it:

    prometheus.NewCounterVec(prometheus.CounterOpts{
        Name: "operator_task_terminal_total",
        Help: "...",
    }, []string{"kind", "state", "stateReason"}),

so the name and the label set come from ONE parse of ONE call. Deliberately not
a vendored golden file: a hand-refreshed snapshot rots in exactly the direction
this issue documents (stage_values_allowlist.txt was 28 days stale and still
listed the pre-v2.0.0 16-value enum), and deliberately not live Prometheus: a
labelled vec that has never been written to has zero series and would be
indistinguishable from a deleted one.

FAIL CLOSED. This is the whole point of the script and it is where the two
guards before it went wrong - check_metric_provenance.py reported OK while 9
rules were dark, and reconcile_metric_provenance.py's nightly 03:23 UTC sweep
sat inside the Grafana blackout window of #94. So there is no path here where
"could not see" is reported as "fine":

  - a clone that fails after _CLONE_ATTEMPTS tries is a HARD FAILURE, not the
    neutral skip reconcile_metric_provenance.py takes. That script's skip is
    defensible because its job is to police an allowlist that is stale-until-
    proven-otherwise; this one's job is to prove a selector resolves, and an
    unproven selector is exactly the defect;
  - a metric selected but absent from scripts/metrics_allowlist.txt fails: no
    section means no producer repo means no derivable label set;
  - a metric in a derivable section that no clone declares fails (the metric
    name itself is dead - reconcile_metric_provenance.py owns the fix, but this
    script must not report OK on a query it could not resolve);
  - a metric whose every declaration builds its label slice from a variable
    rather than a `[]string{...}` literal fails as UNRESOLVED.

The ONE thing not checked is the "external" allowlist section (kube-state-
metrics, kubelet, and the forward-looking OTel entry - see
reconcile_metric_provenance.py's SECTION_REPO). Those are not emitted by any
tatara repo, so there is no source to derive from. That is an explicit,
reviewed exemption carried in ONE place shared with the reconcile script, not a
silent fallthrough, and the count of exempted metrics is printed on every run.

WHAT COUNTS AS "NAMING A LABEL". Two forms, both metric-bound:

  1. a label matcher inside a selector body - `metric{label="v"}`, `=~`, `!=`,
     `!~`. Unambiguous: the label belongs to that metric.
  2. a `by (...)` grouping clause, but ONLY when the expression selects exactly
     one allowlisted metric and uses no relabelling function. Grouping by a
     label that does not exist is not dark - it collapses every series into one
     group with the label set to "" - but it silently destroys the dimension
     the rule claims to report and blanks any {{ index $labels "..." }} the
     annotation templates off it. `without (...)` is deliberately NOT checked:
     removing a label that is not there is a genuine no-op and defensive
     `without (le)` is idiomatic.

INFRASTRUCTURE LABELS. _INFRA_LABELS are attached by the scrape pipeline or by
the Prometheus client library, never by the producer's `[]string{...}`, so they
are legal on every metric. This is a fixed, spec-defined set (kube-prometheus-
stack target labels plus the client library's synthesised `le`/`quantile`), not
a per-metric list of this repo's own making, so it does not drift with the
producers the way a snapshot would.

Run: python3 scripts/check_label_provenance.py
Exit 0 = clean, 1 = a label nobody emits / an unresolvable metric / a clone that
never succeeded, 2 = usage/parse error.
"""

from __future__ import annotations

import glob
import json
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

from check_metric_provenance import dashboard_queries, metric_names
from reconcile_metric_provenance import (
    REPOS,
    SECTION_REPO,
    parse_allowlist_sections,
)

# Histogram/summary suffixes: a query selects metric_bucket/_sum/_count, the Go
# source declares the base name. Must stay in step with check_metric_provenance.
# _SUFFIXES - test_check_label_provenance.py asserts the two agree.
_SUFFIXES = ("_bucket", "_sum", "_count")

# Labels the scrape pipeline or the client library attaches, which therefore
# never appear in a producer's `[]string{...}` and are legal on every metric:
#   job/instance/namespace/pod/container/endpoint/service/node/cluster and the
#     prometheus/prometheus_replica pair - kube-prometheus-stack ServiceMonitor
#     target labels, applied by relabeling at scrape time;
#   le / quantile - synthesised by the client library for histogram buckets and
#     summary quantiles, not part of the declared label set;
#   __name__ - the metric name itself;
#   exported_* - the honor_labels collision prefix Prometheus applies when a
#     scraped series already carries a target label name.
_INFRA_LABELS = frozenset(
    {
        "__name__",
        "cluster",
        "container",
        "endpoint",
        "instance",
        "job",
        "le",
        "namespace",
        "node",
        "pod",
        "prometheus",
        "prometheus_replica",
        "quantile",
        "service",
    }
)
_EXPORTED_PREFIX = "exported_"

# Anchors a Prometheus metric constructor call. Identical to
# reconcile_metric_provenance._CTOR by design - the label slice is read off the
# same call that yields the name, so the two must anchor on the same thing.
_CTOR = re.compile(
    r"prometheus\.(?:New\w+|MustNewConstMetric)\(|promauto\.\w+\.New\w+\("
)
# The constructor's Name/name field, e.g. `Name: "operator_task_stage"`.
_NAME_FIELD = re.compile(r'(?:Name|name):\s*"([a-z][a-z0-9_]+)"')
# The constructor's positional string literal, e.g. NewDesc("operator_pushed_runs", ...).
_POSITIONAL = re.compile(r'NewDesc\(\s*"([a-z][a-z0-9_]+)"')
# A constructor that takes a variable-label slice at all. Everything else
# (NewGauge, NewCounter, NewHistogram, NewSummary) is a single unlabelled
# series, so an empty declared set is the CORRECT answer, not an unresolved one.
_LABELLED_CTOR = re.compile(r"New\w*Vec\(|NewDesc\(|MustNewConstMetric\(")
# The `[]string{"a", "b"}` variable-label slice, and `prometheus.Labels{"a": "b"}`
# const labels, which are also real labels on the resulting series.
_STRING_SLICE = re.compile(r"\[\]string\{([^{}]*)\}")
_CONST_LABELS = re.compile(r"prometheus\.Labels\{([^{}]*)\}")
_QUOTED = re.compile(r'"([^"]*)"')
_CONST_LABEL_KEY = re.compile(r'"([^"]*)"\s*:')
# A literal `nil` in ARGUMENT position - the explicit "no variable labels" form.
_NIL_ARG = re.compile(r"[(,]\s*nil\s*[,)]")

# `metric{...}` - a metric name followed by its label-selector body. Same shape
# as check_metric_provenance._SELECTOR: the label names are read per metric, so
# every finding names the metric it belongs to.
_SELECTOR = re.compile(r"(?<![A-Za-z0-9_:])([a-z_][a-z0-9_]*)\s*\{([^}]*)\}")
# A label matcher inside a selector body. All four PromQL matchers, including
# `!=`, which check_metric_provenance's value regex does not cover.
_LABEL_MATCHER = re.compile(
    r'(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)\s*"'
)
# A `by (a, b)` grouping clause. `without` is deliberately excluded - see the
# module docstring.
_BY_CLAUSE = re.compile(r"\bby\s*\(([^()]*)\)")
# Relabelling constructs that can MINT a label name that no producer declares,
# which makes any grouping label in the same expression unbindable.
_RELABEL = re.compile(r"\blabel_replace\s*\(|\blabel_join\s*\(|\blabel_format\b")

# How many times to retry a clone before failing closed. A transient blip is
# retried; a repo that is genuinely unreachable fails the run rather than
# silently un-checking every metric it produces.
_CLONE_ATTEMPTS = 3


def _base_name(metric: str) -> str:
    """The declared metric name behind a _bucket/_sum/_count query suffix."""
    for suffix in _SUFFIXES:
        if metric.endswith(suffix):
            return metric[: -len(suffix)]
    return metric


def constructor_call(text: str, start: int) -> str | None:
    """The full source text of the constructor call whose match begins at start.

    Paren-matched rather than a fixed line window: a Help string can run to
    several concatenated lines, and the label slice sits AFTER it, so the
    _WINDOW heuristic reconcile_metric_provenance.py uses for the name alone is
    not wide enough to reach the labels reliably. String literals are tracked so
    a parenthesis inside a Help string cannot close the call early.
    """
    try:
        i = text.index("(", start)
    except ValueError:
        return None
    depth = 0
    in_string = False
    j = i
    while j < len(text):
        char = text[j]
        if in_string:
            if char == "\\":
                j += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
        j += 1
    return None


def derive_metric_labels(
    repo_dir: pathlib.Path,
) -> tuple[dict[str, set[str]], set[str]]:
    """(labels, unresolved) for every Prometheus metric declared under repo_dir.

    labels[name] is the declared variable-label set (plus any const labels),
    unioned over every declaration of that name. unresolved holds names whose
    label slice is built from a variable rather than a `[]string{...}` literal
    and so could not be read; a name that resolves at ANY declaration site is
    resolved and does not appear there.
    """
    labels: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    for path in sorted(repo_dir.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for match in _CTOR.finditer(text):
            call = constructor_call(text, match.start())
            if call is None:
                continue
            # _CTOR always ends on the call's "(" and constructor_call starts
            # from it, so dropping one of the two rebuilds the call verbatim -
            # which is what _POSITIONAL needs to see (`NewDesc("name"`).
            span = match.group(0)[:-1] + call
            found = _POSITIONAL.search(span) or _NAME_FIELD.search(call)
            if not found:
                continue  # a dynamic name - the producer declares it statically elsewhere
            name = found.group(1)
            slices = _STRING_SLICE.findall(call)
            if slices:
                # The variable-label slice is the constructor's last []string
                # argument (NewXVec(opts, []string{...}); NewDesc(name, help,
                # []string{...}, constLabels)).
                declared = set(_QUOTED.findall(slices[-1]))
            elif not _LABELLED_CTOR.search(match.group(0)):
                declared = set()  # an unlabelled single-series collector
            elif _NIL_ARG.search(call):
                # Explicitly no variable labels, e.g. NewDesc(name, help, nil, nil).
                # Matched as an ARGUMENT, not a bare word anywhere in the call: a
                # Help string that happened to contain "nil" must not be read as
                # "this metric declares no labels", which is the unsafe direction.
                declared = set()
            else:
                unresolved.add(name)
                continue
            for const in _CONST_LABELS.findall(call):
                declared |= set(_CONST_LABEL_KEY.findall(const))
            labels.setdefault(name, set()).update(declared)
    return labels, unresolved - set(labels)


def selector_labels(expr: str) -> dict[str, set[str]]:
    """{metric: {label names it is selected on}} for one PromQL expression."""
    out: dict[str, set[str]] = {}
    for metric, body in _SELECTOR.findall(expr):
        for label in _LABEL_MATCHER.findall(body):
            out.setdefault(_base_name(metric), set()).add(label)
    return out


def grouping_labels(expr: str) -> dict[str, set[str]]:
    """{metric: {label names it is grouped BY}} - only when the binding is certain.

    Returns {} unless the expression selects exactly one metric and uses no
    relabelling construct. A `by (...)` clause in a join, a binary operation
    over two metrics, or an expression that mints labels with label_replace has
    no single owner, and guessing one would produce the false positives that
    make a guard get switched off.
    """
    clauses = _BY_CLAUSE.findall(re.sub(r"\{[^}]*\}", "", expr))
    if not clauses or _RELABEL.search(expr):
        return {}
    metrics = metric_names(expr)
    if len(metrics) != 1:
        return {}
    metric = _base_name(next(iter(metrics)))
    grouped = {
        label.strip()
        for clause in clauses
        for label in clause.split(",")
        if label.strip()
    }
    return {metric: grouped} if grouped else {}


class Violation:
    """One label-provenance finding. kind is "label" for a label the metric does
    not declare, or one of the three fail-closed kinds for a metric whose label
    set could not be established at all."""

    def __init__(self, path: str, context: str, kind: str, metric: str, detail: str):
        self.path = path
        self.context = context
        self.kind = kind
        self.metric = metric
        self.detail = detail

    def __str__(self) -> str:
        head = f"{self.path}: {self.context}"
        if self.kind == "label":
            return (
                f"{head} names label `{self.detail}` on `{self.metric}`, which the "
                f"producer does not declare on that metric. A positive selector on a "
                f"label the metric does not carry matches NOTHING, forever - with "
                f'default_no_data_state: "OK" the rule reports OK and the panel renders '
                f"empty, silently. A negative selector on one matches EVERYTHING, so "
                f"the rule goes falsely noisy instead. Repoint it onto a label the "
                f"metric actually declares, or fix the producer."
            )
        if self.kind == "unallowlisted":
            return (
                f"{head} selects `{self.metric}`, which is not in "
                f"scripts/metrics_allowlist.txt, so no producer repo owns it and its "
                f"label set cannot be derived. This check fails closed: an unresolvable "
                f"selector is the defect, not an exemption. Add the metric to the "
                f"allowlist under its producer's section (see check_metric_provenance.py)."
            )
        if self.kind == "underivable":
            return (
                f"{head} selects `{self.metric}`, which {self.detail} declares nowhere "
                f"in its Go source. The metric name itself is dead - "
                f"reconcile_metric_provenance.py owns removing it from the allowlist - "
                f"but the query must be repointed either way, and this check will not "
                f"report OK on a selector it could not resolve."
            )
        return (
            f"{head} selects `{self.metric}`, whose label slice {self.detail} builds "
            f"from a variable rather than a `[]string{{...}}` literal, so the declared "
            f"label set could not be read. Fail closed: make the producer declare the "
            f"slice literally, or this check cannot prove the query resolves."
        )


def alert_queries(path: str) -> list[tuple[str, str]]:
    """(context, PromQL) for every Prometheus query in an alert file."""
    data = yaml.safe_load(pathlib.Path(path).read_text())
    if not data or not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    for rule in data.get("rules") or []:
        context = f'rule "{rule.get("name", "<unnamed>")}"'
        for query in rule.get("queries") or []:
            if (query.get("query_type") or "prometheus") != "prometheus":
                continue  # loki streams are not metrics
            expression = query.get("expression")
            if expression:
                out.append((context, expression))
    return out


def check_expr(
    path: str,
    context: str,
    expr: str,
    sections: dict[str, str],
    declared: dict[str, set[str]],
    unresolved: set[str],
) -> list[Violation]:
    """Every label-provenance violation in one PromQL expression."""
    named: dict[str, set[str]] = {}
    for source in (selector_labels(expr), grouping_labels(expr)):
        for metric, labels in source.items():
            named.setdefault(metric, set()).update(labels)

    out: list[Violation] = []
    for metric in sorted(named):
        section = sections.get(metric)
        if section is None:
            out.append(Violation(path, context, "unallowlisted", metric, ""))
            continue
        repo = SECTION_REPO.get(section)
        if repo is None:
            continue  # exempt section - not emitted by any tatara repo, nothing to derive
        if metric in unresolved:
            out.append(Violation(path, context, "unresolved", metric, repo))
            continue
        carried = declared.get(metric)
        if carried is None:
            out.append(Violation(path, context, "underivable", metric, repo))
            continue
        for label in sorted(named[metric]):
            if label in _INFRA_LABELS or label.startswith(_EXPORTED_PREFIX):
                continue
            if label not in carried:
                out.append(Violation(path, context, "label", metric, label))
    return out


def check_paths(
    paths: list[str],
    sections: dict[str, str],
    declared: dict[str, set[str]],
    unresolved: set[str],
) -> list[Violation]:
    """Every violation across alert files and dashboards, deduplicated - one
    panel can repeat a metric across targets."""
    out: list[Violation] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for path in paths:
        queries = (
            dashboard_queries(path) if path.endswith(".json") else alert_queries(path)
        )
        for context, expr in queries:
            for violation in check_expr(
                path, context, expr, sections, declared, unresolved
            ):
                key = (
                    violation.path,
                    violation.context,
                    violation.kind,
                    violation.metric,
                    violation.detail,
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(violation)
    return out


def clone_repo(name: str, url: str, dest: pathlib.Path) -> bool:
    """Shallow-clone url@main into dest, retrying a transient failure.

    Unlike reconcile_metric_provenance.clone_repo, a definitive failure here is
    NOT swallowed: the caller turns it into a hard failure (see the FAIL CLOSED
    paragraph in the module docstring).
    """
    last = ""
    for attempt in range(1, _CLONE_ATTEMPTS + 1):
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
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            last = str(exc)
            print(
                f"check_label_provenance: clone of {name} failed "
                f"(attempt {attempt}/{_CLONE_ATTEMPTS}): {exc}",
                file=sys.stderr,
            )
    print(
        f"::error::check_label_provenance: could not clone {name} after "
        f"{_CLONE_ATTEMPTS} attempts ({last}). This is a HARD FAILURE, not a skip: "
        "without the producer source every label on every metric it emits is "
        "unverified, and reporting OK on that is the exact silent-green failure "
        "this check exists to end.",
        file=sys.stderr,
    )
    return False


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _default_paths() -> list[str]:
    return sorted(
        glob.glob(str(_root() / "alerts" / "*.yaml"))
        + glob.glob(str(_root() / "dashboards" / "*.json"))
    )


def main(argv: list[str]) -> int:
    paths = argv[1:] or _default_paths()
    if not paths:
        print(
            "check_label_provenance: no alert or dashboard files found", file=sys.stderr
        )
        return 2
    try:
        sections = parse_allowlist_sections(
            _root() / "scripts" / "metrics_allowlist.txt"
        )
    except OSError as exc:
        print(f"check_label_provenance: {exc}", file=sys.stderr)
        return 2

    declared: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="label-provenance-") as tmp:
        for name, url in REPOS.items():
            dest = pathlib.Path(tmp) / name
            if not clone_repo(name, url, dest):
                return 1
            repo_labels, repo_unresolved = derive_metric_labels(dest)
            for metric, labels in repo_labels.items():
                declared.setdefault(metric, set()).update(labels)
            unresolved |= repo_unresolved
        unresolved -= set(declared)

        try:
            violations = check_paths(paths, sections, declared, unresolved)
        except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
            print(f"check_label_provenance: {exc}", file=sys.stderr)
            return 2

    exempt = sorted(
        {m for m, s in sections.items() if SECTION_REPO.get(s) is None}
    )
    if violations:
        print(
            f"FAIL: {len(violations)} alert rule(s) / dashboard panel(s) name a label "
            f"the producing metric does not carry, or select a metric whose label set "
            f"could not be derived:\n"
        )
        for violation in violations:
            print(f"  - {violation}")
        print(
            f"\n({len(declared)} metric name(s) derived from {len(REPOS)} producer "
            f"repo(s); {len(exempt)} allowlisted metric(s) in the exempt 'external' "
            f"section were not derivable by design.)"
        )
        return 1
    print(
        f"OK: every label named in {len(paths)} alert file(s)/dashboard(s) is declared "
        f"by its producing metric ({len(declared)} metric name(s) derived from "
        f"{len(REPOS)} producer repo(s); {len(exempt)} allowlisted metric(s) in the "
        f"exempt 'external' section were not derivable by design)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
