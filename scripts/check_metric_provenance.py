#!/usr/bin/env python3
"""Fail CI when an alert rule or a dashboard panel selects a metric nobody emits.

The pre-2026-07-12 alert set carried 8 rules keyed on Task fields the task-centric
redesign deletes. Because every alert file sets default_no_data_state: "OK", a rule
whose series vanishes does not fire and does not go stale - it reports OK forever.
Two of the 8 were the CD-cascade alerts, so the merge/deploy path to a
cluster-admin-scoped runner had zero coverage while every dashboard read green.

DASHBOARDS ARE THE SAME FAILURE CLASS (2026-07-13). A panel whose metric is deleted
renders EMPTY, SILENTLY, FOREVER, with no CI signal at all - there is not even a
NoData state to mis-configure. dashboards/operator.json and dashboards/task-delivery.json
shipped past the first cut of this redesign still querying tatara_issue_state,
tatara_tasks_inflight, tatara_cd_resolved_total and the systemic-group counters, all of
which the redesign deletes. So this check walks dashboards/*.json too: every
panels[].targets[].expr, every nested panels[].panels[].targets[].expr (row-collapsed
panels), and every templating.list[] query variable that carries PromQL.

Every metric name found must appear in scripts/metrics_allowlist.txt. Loki queries
(alert query_type: loki; a panel target or template variable whose datasource type is
not prometheus) are out of scope: they select log streams, not metrics.

THIS FILE OWNS ONE DIMENSION: the metric NAME. A PromQL selector has three
(name, label name, label value) and the other two are checked in
reconcile_metric_provenance.py, because both are DERIVED from a producer clone
rather than from a hand-maintained snapshot in this repo. Until #100 the label
VALUE sweep lived here against scripts/stage_values_allowlist.txt; that file was
a copy of the operator's enums and had rotted across two contract versions
without a single check going red, which is the whole reason the derivation moved.

This file is therefore also the SHARED PARSER. selector_labels() and
iter_expressions() are the surface reconcile_metric_provenance.py consumes, so
"every Prometheus expression this repo ships" has exactly one definition and two
walks cannot drift apart.

Exit 0 = clean, 1 = unknown metric, 2 = usage/parse error.
"""

from __future__ import annotations

import glob
import json
import pathlib
import re
import sys
from typing import Iterator, NamedTuple

import yaml

# An identifier that is NOT immediately followed by "(" (a function call) and is not a
# bare PromQL keyword. Metric names in this repo are lower_snake_case.
_IDENT = re.compile(r"(?<![A-Za-z0-9_:.\"])([a-z_][a-z0-9_]*)(?![A-Za-z0-9_(])")

# PromQL keywords / modifiers that look like identifiers but are not metrics.
_KEYWORDS = frozenset(
    {
        "and",
        "or",
        "unless",
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "offset",
        "bool",
        "le",
        "start",
        "end",
        "atan2",
    }
)

# Histogram/summary suffixes: alert on _bucket/_sum/_count, allowlist the base name.
_SUFFIXES = ("_bucket", "_sum", "_count")

# One label matcher inside a selector body. The operator alternatives are ordered
# longest-first: `=~` and `!~` must win over `=` and `!=`, and the pre-#100 regex
# (`(?:=~?|!~?)`) silently matched NOTHING for `label!="v"` because `!~?` consumed
# the "!" and then demanded a quote.
_MATCHER = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"([^"]*)"')

# `metric{...}` - a metric name followed by its label-selector body. Carries the
# metric so both label checks can be metric-aware (an overloaded label name means
# nothing without knowing which metric it is on).
_SELECTOR = re.compile(r"(?<![A-Za-z0-9_:])([a-z_][a-z0-9_]*)\s*\{([^}]*)\}")

# A Grafana template variable: never a literal under any operator. It must NOT be a
# bare `$` - that is also the regex end-anchor in `=~"^(done|rejected)$"`, and
# treating the anchor as a variable silently exempts every anchored matcher from the
# value sweep.
_TEMPLATE_VAR = re.compile(r"\$\{?[A-Za-z_]|\[\[")
# A real PromQL regex rather than a literal, once the whole-match anchors below are
# stripped. Only meaningful for the =~ / !~ operators: under `=` the value is an
# exact match, so a `.` in it is a dot and screening it out would silently exempt
# the likeliest typo class (stateReason="mr-merged.externally") from the value sweep.
_REGEX_PATTERN = re.compile(r"[.*+?()\[\]{}^$|\\]")
# `^(a|b)$` and `^a$` are ordinary PromQL. Left whole, every alternative carries a
# metacharacter and the entire matcher goes value-unchecked.
_ANCHORED_GROUP = re.compile(r"^\^\((.*)\)\$$", re.S)
_ANCHORED_BARE = re.compile(r"^\^(.*)\$$", re.S)

# Grafana template-variable queries. label_values(<expr>, <label>) and query_result(<expr>)
# carry PromQL; label_values(<label>), metrics(...) and label_names(...) do not.
_TPL_LABEL_VALUES = re.compile(r"^\s*label_values\s*\((.*)\)\s*$", re.S)
_TPL_QUERY_RESULT = re.compile(r"^\s*query_result\s*\((.*)\)\s*$", re.S)
_TPL_NO_METRIC = re.compile(r"^\s*(?:metrics|label_names)\s*\(", re.S)


def metric_names(expr: str) -> set[str]:
    """Every metric name selected by a PromQL expression."""
    # Drop label-selector bodies and string literals: label VALUES are not metrics.
    stripped = re.sub(r"\{[^}]*\}", "", expr)
    stripped = re.sub(r"\"[^\"]*\"", "", stripped)
    # Drop duration/number literals like [15m], 5e-6, 0.95.
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)
    # Drop grouping/join-modifier clauses: "by (task)", "without (le)",
    # "on (project)", "ignoring (pod)", "group_left(x,y)" all take a parenthesized
    # label LIST, not a metric, and left in place they would otherwise fall
    # through as bare identifiers below.
    stripped = re.sub(
        r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)",
        "",
        stripped,
    )
    # An aggregation operator followed by whitespace then "(" (e.g. "max  (metric)"
    # once its "by (...)" clause above is removed) is a function call, same as
    # "max(metric)" with no space - collapse the whitespace so the _IDENT
    # lookahead below excludes it uniformly.
    stripped = re.sub(r"([A-Za-z_][A-Za-z0-9_]*)\s+\(", r"\1(", stripped)
    out: set[str] = set()
    for m in _IDENT.finditer(stripped):
        name = m.group(1)
        if name in _KEYWORDS:
            continue
        for suffix in _SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        out.add(name)
    return out


class Selector(NamedTuple):
    """One label matcher, with the metric it constrains.

    `values` holds only LITERALS: a `$var` (Grafana template variable) and a real
    regex pattern are both dropped, because neither can be membership-tested
    against a closed set. The label is still reported with an empty value set - a
    variable-valued matcher on a label name that does not exist is exactly as dark
    as a literal one, so the NAME check must still see it.
    """

    metric: str
    label: str
    op: str
    values: frozenset[str]

    @property
    def positive(self) -> bool:
        """False for `!=` / `!~`.

        A negative matcher on a label the metric does not carry matches EVERY
        series, so it is a no-op filter, not a dark selector. reconcile_metric_
        provenance.py relies on this distinction: hard-failing it would break the
        forward-compatible idiom of writing the matcher before the producer adds
        the label (see ROADMAP, tatara-memory#92).
        """
        return not self.op.startswith("!")


def selector_labels(expr: str) -> list[Selector]:
    """Every label matcher in a PromQL expression, with its metric and operator.

    This reports EVERY label name, not a fixed list: which labels are actually
    checkable is the consumer's decision (reconcile_metric_provenance.py, which
    knows what the producer clone declares). The pre-#100 extractor hardcoded four
    names, so the post-v2.0.0 vocabulary was invisible to it and no label NAME was
    ever validated at all.

    A regex `=~"a|b|c"` matcher is split on `|` into candidate values; an `=`
    matcher is one value even if it contains a bar.

    KNOWN BLIND SPOT: _SELECTOR's body is `[^}]*`, so a Grafana `${var}` matcher
    terminates it early and the whole selector goes unreported - not just its
    values. `$var` and `[[var]]` are fine. No alert or dashboard uses the braced
    spelling today; widening the regex for a shape nothing emits would add
    unexercised machinery to the one parser both checks depend on.
    """
    out: list[Selector] = []
    for metric, body in _SELECTOR.findall(expr):
        for suffix in _SUFFIXES:
            if metric.endswith(suffix):
                metric = metric[: -len(suffix)]
                break
        for label, op, raw in _MATCHER.findall(body):
            out.append(Selector(metric, label, op, _literal_values(op, raw)))
    return out


def _literal_values(op: str, raw: str) -> frozenset[str]:
    """The literal candidate values of one matcher's right-hand side.

    An `=` / `!=` value is exactly one literal, whatever characters it contains. A
    `=~` / `!~` value is a regex: whole-match anchors are stripped, plain
    alternation is split on `|`, and anything still carrying a metacharacter is
    dropped - it cannot be membership-tested against a closed set, and guessing
    would fail on correct work.
    """
    if _TEMPLATE_VAR.search(raw):
        return frozenset()
    if not op.endswith("~"):
        return frozenset({raw.strip()}) if raw.strip() else frozenset()
    m = _ANCHORED_GROUP.match(raw) or _ANCHORED_BARE.match(raw)
    if m:
        raw = m.group(1)
    return frozenset(
        v.strip()
        for v in raw.split("|")
        if v.strip() and not _REGEX_PATTERN.search(v)
    )


def template_expr(query: str) -> str:
    """The PromQL inside a Grafana template-variable query, or "" if it carries none."""
    if _TPL_NO_METRIC.match(query):
        return ""
    m = _TPL_QUERY_RESULT.match(query)
    if m:
        return m.group(1)
    m = _TPL_LABEL_VALUES.match(query)
    if m:
        # label_values(<expr>, <label>) -> the expr; label_values(<label>) -> no metric.
        head, sep, _ = m.group(1).rpartition(",")
        return head if sep else ""
    return query  # a bare PromQL variable query


def _is_prometheus(datasource: object) -> bool:
    """A Grafana datasource ref is Prometheus unless it says otherwise (loki, etc)."""
    if isinstance(datasource, dict) and datasource.get("type"):
        return datasource["type"] == "prometheus"
    return True


def dashboard_queries(path: str) -> list[tuple[str, str]]:
    """(context, PromQL) for every Prometheus expression in a Grafana dashboard JSON."""
    data = json.loads(pathlib.Path(path).read_text())
    out: list[tuple[str, str]] = []

    def walk(panels: list[dict]) -> None:
        for panel in panels:
            title = panel.get("title") or "<untitled>"
            for target in panel.get("targets") or []:
                ds = target.get("datasource") or panel.get("datasource")
                if not _is_prometheus(ds):
                    continue  # loki targets select log streams, not metrics
                expr = target.get("expr")
                if expr:
                    out.append((f'panel "{title}"', expr))
            walk(panel.get("panels") or [])  # row-collapsed panels

    walk(data.get("panels") or [])
    for var in (data.get("templating") or {}).get("list") or []:
        if var.get("type") != "query" or not _is_prometheus(var.get("datasource")):
            continue
        query = var.get("query")
        if isinstance(query, dict):
            query = query.get("query")
        if not isinstance(query, str):
            continue
        expr = template_expr(query)
        if expr.strip():
            out.append((f'variable "{var.get("name", "<unnamed>")}"', expr))
    return out


def _rule_queries(rule: dict) -> list[str]:
    """Every Prometheus expression in one alert rule (loki streams are not metrics)."""
    return [
        q.get("expression") or ""
        for q in rule.get("queries") or []
        if (q.get("query_type") or "prometheus") == "prometheus"
    ]


def alert_queries(path: str) -> list[tuple[str, str]]:
    """(context, PromQL) for every Prometheus expression in an alert rule file."""
    data = yaml.safe_load(pathlib.Path(path).read_text())
    if not data or not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    for rule in data.get("rules") or []:
        context = f'rule "{rule.get("name", "<unnamed>")}"'
        for expr in _rule_queries(rule):
            if expr:
                out.append((context, expr))
    return out


def iter_expressions(paths: list[str]) -> Iterator[tuple[str, str, str]]:
    """(path, context, PromQL) for every Prometheus expression this repo ships.

    The single definition of that set. reconcile_metric_provenance.py's label
    checks consume it rather than re-walking alerts/*.yaml and dashboards/*.json,
    so the three dimensions can never disagree about what they cover.
    """
    for path in paths:
        queries = (
            dashboard_queries(path) if path.endswith(".json") else alert_queries(path)
        )
        for context, expr in queries:
            yield path, context, expr


def load_allowlist(path: str) -> set[str]:
    out: set[str] = set()
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


class Violation:
    def __init__(self, path: str, context: str, value: str):
        self.path = path
        self.context = context
        self.value = value

    def __str__(self) -> str:
        return (
            f"{self.path}: {self.context} selects `{self.value}`, which is not in "
            f"scripts/metrics_allowlist.txt. Either the metric is not emitted (an alert on "
            f"an absent series reports OK forever; a dashboard panel renders empty forever, "
            f"silently, with no CI signal - see the file header), or the allowlist needs the "
            f"new name adding in the same PR as the service that emits it."
        )


def lint_expr(path: str, context: str, expr: str, allowed: set[str]) -> list[Violation]:
    """Every metric-name violation in one PromQL expression."""
    return [
        Violation(path, context, name)
        for name in sorted(metric_names(expr))
        if name not in allowed
    ]


def lint_rule(path: str, rule: dict, allowed: set[str]) -> Violation | None:
    context = f'rule "{rule.get("name", "<unnamed>")}"'
    for expr in _rule_queries(rule):
        violations = lint_expr(path, context, expr, allowed)
        if violations:
            return violations[0]
    return None


def lint_file(path: str, allowed: set[str]) -> list[Violation]:
    data = yaml.safe_load(pathlib.Path(path).read_text())
    if not data or not isinstance(data, dict):
        return []
    out = []
    for rule in data.get("rules") or []:
        v = lint_rule(path, rule, allowed)
        if v is not None:
            out.append(v)
    return out


def lint_dashboard(path: str, allowed: set[str]) -> list[Violation]:
    """Every violation in a dashboard, deduplicated - one panel can repeat a metric."""
    out: list[Violation] = []
    seen: set[tuple[str, str]] = set()
    for context, expr in dashboard_queries(path):
        for v in lint_expr(path, context, expr, allowed):
            key = (v.context, v.value)
            if key in seen:
                continue
            seen.add(key)
            out.append(v)
    return out


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    paths = argv[1:] or sorted(
        glob.glob(str(_root() / "alerts" / "*.yaml"))
        + glob.glob(str(_root() / "dashboards" / "*.json"))
    )
    if not paths:
        print(
            "check_metric_provenance: no alert or dashboard files found",
            file=sys.stderr,
        )
        return 2
    try:
        allowed = load_allowlist(str(_root() / "scripts" / "metrics_allowlist.txt"))
        violations: list[Violation] = []
        alerts = dashboards = 0
        for path in paths:
            if path.endswith(".json"):
                dashboards += 1
                violations += lint_dashboard(path, allowed)
            else:
                alerts += 1
                violations += lint_file(path, allowed)
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"check_metric_provenance: {exc}", file=sys.stderr)
        return 2
    if violations:
        print(
            f"FAIL: {len(violations)} alert rule(s) / dashboard panel(s) select a metric "
            f"nobody emits:\n"
        )
        for v in violations:
            print(f"  - {v}")
        return 1
    print(
        f"OK: {alerts} alert file(s) + {dashboards} dashboard(s) select only allowlisted "
        f"metric NAMES. Label names and label values are checked against the producer "
        f"clones by scripts/reconcile_metric_provenance.py."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
