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

It also validates every stageReason= / stage= / kind= / agent_kind= label VALUE used
in an expression against the closed sets in scripts/stage_values_allowlist.txt
(contract F.1, F.5, A.4). The metric-name check alone cannot catch a rule that
filters on a dead label value: the metric still exists and the rule still passes the
name check, but the value never appears in the series, so the rule reports OK forever
- the exact same failure class one level down. The value sweep is METRIC-AWARE: `kind`
is an overloaded label name (operator_scm_writes_total{kind="write"} is a verb class,
not a Task kind), so a metric may be exempted from one label's closed set via a
`## <label>:exempt-metrics` section in stage_values_allowlist.txt. The default is to
CHECK - a new metric that overloads a closed-set label fails CI until someone
explicitly exempts it.

Exit 0 = clean, 1 = unknown metric or label value, 2 = usage/parse error.
"""

from __future__ import annotations

import glob
import json
import pathlib
import re
import sys

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

# Label name -> which closed set (in stage_values_allowlist.txt) its values must
# belong to. Matches `label="value"` or `label=~"value1|value2"`.
_LABEL_VALUE = re.compile(
    r'\b(stageReason|stage|kind|agent_kind)\s*(?:=~?|!~?)\s*"([^"]*)"'
)

# `metric{...}` - a metric name followed by its label-selector body. Used to make the
# closed-set label sweep metric-aware (see the module docstring).
_SELECTOR = re.compile(r"(?<![A-Za-z0-9_:])([a-z_][a-z0-9_]*)\s*\{([^}]*)\}")

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


def label_values(expr: str) -> dict[str, dict[str, set[str]]]:
    """Every stageReason=/stage=/kind=/agent_kind= label value selected, by metric.

    Returns {metric: {label: {values}}}. The metric is carried because `kind` is an
    overloaded label name and its closed set only applies to some metrics.

    A regex `=~"a|b|c"` selector is split on `|` into individual candidate values;
    PromQL regex metacharacters beyond plain alternation are left as-is and will
    simply fail to match anything in the allowlist (a false positive is preferable
    to silently skipping a real value). A `$var` value is a Grafana template variable,
    not a literal, and is skipped.
    """
    out: dict[str, dict[str, set[str]]] = {}
    for metric, body in _SELECTOR.findall(expr):
        for suffix in _SUFFIXES:
            if metric.endswith(suffix):
                metric = metric[: -len(suffix)]
                break
        for label, raw in _LABEL_VALUE.findall(body):
            for val in raw.split("|"):
                val = val.strip()
                if not val or val.startswith("$"):
                    continue
                out.setdefault(metric, {}).setdefault(label, set()).add(val)
    return out


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


def load_allowlist(path: str) -> set[str]:
    out: set[str] = set()
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


def load_stage_values(path: str) -> dict[str, set[str]]:
    """Parse stage_values_allowlist.txt into {label_name: {allowed values}}.

    Format: `## <label_name>` section headers, then one value per line.
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


class Violation:
    def __init__(self, path: str, context: str, kind: str, value: str):
        self.path = path
        self.context = context
        self.kind = kind
        self.value = value

    def __str__(self) -> str:
        if self.kind == "metric":
            return (
                f"{self.path}: {self.context} selects `{self.value}`, which is not in "
                f"scripts/metrics_allowlist.txt. Either the metric is not emitted (an alert on "
                f"an absent series reports OK forever; a dashboard panel renders empty forever, "
                f"silently, with no CI signal - see the file header), or the allowlist needs the "
                f"new name adding in the same PR as the service that emits it."
            )
        label, value = self.kind, self.value
        return (
            f'{self.path}: {self.context} selects {label}="{value}", which is not in the '
            f"closed set for {label} in scripts/stage_values_allowlist.txt (contract F.1/F.5/A.4). "
            f"A query filtering on a dead label value reports OK / renders empty forever, same as "
            f"a dead metric name - the metric-name check alone cannot catch this. If the label is "
            f"overloaded on this metric (kind= is), exempt the metric under "
            f"`## {label}:exempt-metrics`."
        )


def lint_expr(
    path: str,
    context: str,
    expr: str,
    allowed: set[str],
    stage_values: dict[str, set[str]] | None = None,
) -> list[Violation]:
    """Every metric-name and closed-set label-value violation in one PromQL expression."""
    out: list[Violation] = []
    for name in sorted(metric_names(expr)):
        if name not in allowed:
            out.append(Violation(path, context, "metric", name))
    if not stage_values:
        return out
    for metric, labels in sorted(label_values(expr).items()):
        for label, values in sorted(labels.items()):
            allowed_values = stage_values.get(label)
            if allowed_values is None:
                continue  # label not in the closed-set scope (e.g. a non-contract label)
            if metric in stage_values.get(f"{label}:exempt-metrics", set()):
                continue  # the label name is overloaded on this metric
            for value in sorted(values):
                if value not in allowed_values:
                    out.append(Violation(path, context, label, value))
    return out


def lint_rule(
    path: str,
    rule: dict,
    allowed: set[str],
    stage_values: dict[str, set[str]] | None = None,
) -> Violation | None:
    context = f'rule "{rule.get("name", "<unnamed>")}"'
    for q in rule.get("queries") or []:
        if (q.get("query_type") or "prometheus") != "prometheus":
            continue  # loki streams are not metrics
        violations = lint_expr(
            path, context, q.get("expression") or "", allowed, stage_values
        )
        if violations:
            return violations[0]
    return None


def lint_file(
    path: str, allowed: set[str], stage_values: dict[str, set[str]] | None = None
) -> list[Violation]:
    data = yaml.safe_load(pathlib.Path(path).read_text())
    if not data or not isinstance(data, dict):
        return []
    out = []
    for rule in data.get("rules") or []:
        v = lint_rule(path, rule, allowed, stage_values)
        if v is not None:
            out.append(v)
    return out


def lint_dashboard(
    path: str, allowed: set[str], stage_values: dict[str, set[str]] | None = None
) -> list[Violation]:
    """Every violation in a dashboard, deduplicated - one panel can repeat a metric."""
    out: list[Violation] = []
    seen: set[tuple[str, str, str]] = set()
    for context, expr in dashboard_queries(path):
        for v in lint_expr(path, context, expr, allowed, stage_values):
            key = (v.context, v.kind, v.value)
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
        stage_values = load_stage_values(
            str(_root() / "scripts" / "stage_values_allowlist.txt")
        )
        violations: list[Violation] = []
        alerts = dashboards = 0
        for path in paths:
            if path.endswith(".json"):
                dashboards += 1
                violations += lint_dashboard(path, allowed, stage_values)
            else:
                alerts += 1
                violations += lint_file(path, allowed, stage_values)
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"check_metric_provenance: {exc}", file=sys.stderr)
        return 2
    if violations:
        print(
            f"FAIL: {len(violations)} alert rule(s) / dashboard panel(s) select a metric or "
            f"label value nobody emits:\n"
        )
        for v in violations:
            print(f"  - {v}")
        return 1
    print(
        f"OK: {alerts} alert file(s) + {dashboards} dashboard(s) select only emitted metrics "
        f"and live label values."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
