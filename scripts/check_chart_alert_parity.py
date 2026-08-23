#!/usr/bin/env python3
"""Fail CI when tatara-operator's chart PrometheusRule alerts on a condition no rule in
this repo watches.

WHY THIS EXISTS. The platform had two alerting planes and only ever delivered on one.
tatara-operator's chart ships a 32-alert PrometheusRule (`prometheusRule.enabled: true` by
default), and this cluster's Prometheus never loaded a single one of them: the
kube-prometheus-stack `ruleSelector` matches `release=prometheus` and tatara-helmfile never
set `prometheusRule.additionalLabels`. Labelling it was the obvious fix and was the wrong
one - the cluster Alertmanager is stock kube-prometheus-stack with `route.receiver: "null"`,
one Watchdog child route, and ONE receiver named `"null"` carrying zero integrations.
Labelling would have loaded 32 rules that evaluate, fire, and are discarded: manufactured
coverage, which is worse than the silence it replaced.

So tatara-helmfile sets `prometheusRule.enabled: false` (tatara-helmfile#440) and this
repo - the plane that actually delivers, via the `Tatara` contact point webhooking
`/operator/webhooks/<project>/grafana`, which is the only incident-Task minting path -
becomes the single alerting plane on this cluster.

That leaves the chart file in a NEW ROLE. It stops being a deploy artifact and becomes the
SPECIFICATION of conditions the producer thinks are worth alerting on. `alerts/` is what is
actually alerted. Nothing reconciled the two, which is exactly how the situation arose that
this replaces: `tatara-operator#635` filed `TataraAccountUsageFeedDead` as "never written"
on the strength of a grep against this repo, when it had been written, in the plane that
delivered nothing. A competent audit of the wrong plane is indistinguishable from a real
gap. This script is the reconciliation.

WHAT IT CHECKS. Every metric a chart alert reads must be read by at least one rule in
`alerts/`. That is deliberately a METRIC-level check, not a rule-level one: rule shapes,
thresholds and groupings are this repo's business (several ported rules are better than
their chart originals, and one chart rule is worse than the rule that replaced it - see the
waivers), but a metric no rule here reads is a condition with no witness anywhere.

WAIVERS ARE KEYED AND MUST CARRY A REASON. `scripts/chart_alert_waivers.txt` is
`<ChartAlertName> <metric> # <why>`, and a waiver with no reason is a parse error, not a
pass. A bare waiver is how a coverage gap gets silently blessed; the reason is the only
artifact that lets the next reader disagree with it. The key is the (alert, metric) pair
rather than the metric alone, because two chart alerts can read the same metric with only
one of them legitimately superseded.

CLONE FAILURE IS A HARD FAILURE HERE, unlike reconcile_metric_provenance.py's neutral skip.
That costs nothing: check_label_provenance.py in this same workflow already clones
tatara-operator and already fails closed on it, so the job cannot pass without a successful
tatara-operator clone either way. A neutral skip would buy no availability and only lose
signal, and "could not see" reported as OK is the precise failure this file exists to end.

SCOPE, STATED PLAINLY SO THE NAME DOES NOT IMPLY MORE THAN IT COVERS. This checks the
tatara-operator chart's PrometheusRule and nothing else. tatara-memory's chart also ships a
PrometheusRule, but it is provisioned PER-PROJECT BY THE OPERATOR at runtime and labelled
through `MEMORY_MONITOR_LABELS` - a different mechanism, on a path no `helm template` and no
static file read can see. This script does not cover it and does not pretend to.

Run: python3 scripts/check_chart_alert_parity.py
Exit 0 = clean, 1 = an unwatched condition, 2 = usage/parse/clone error.
"""

from __future__ import annotations

import glob
import pathlib
import re
import subprocess
import sys
import tempfile

from check_label_provenance import alert_queries
from check_metric_provenance import metric_names

OPERATOR_REPO = "https://github.com/szymonrychu/tatara-operator.git"
CHART_RULE_PATH = "charts/tatara-operator/templates/prometheusrule.yaml"
WAIVERS_PATH = pathlib.Path(__file__).resolve().parent / "chart_alert_waivers.txt"

_ALERT_LINE = re.compile(r"^(\s*)-\s*alert:\s*(\S+)\s*$")
_EXPR_LINE = re.compile(r"^(\s*)expr:\s*(.*)$")
# A Go template action. Stripped before the expression reaches metric_names(), which is a
# PromQL parser and would otherwise read `.Values.prometheusRule.sweepSkipWindow` as a
# bare identifier and invent a metric out of it.
_TEMPLATE_ACTION = re.compile(r"\{\{.*?\}\}", re.S)
_WAIVER_LINE = re.compile(r"^(?P<alert>\S+)\s+(?P<metric>\S+)\s*#\s*(?P<reason>.*\S)\s*$")


class Waiver:
    def __init__(self, reason: str):
        self.reason = reason


class Violation:
    """One chart alert reading a metric no rule in alerts/ reads."""

    def __init__(self, alert: str, metric: str):
        self.alert = alert
        self.metric = metric

    def __str__(self) -> str:
        return (
            f"chart alert `{self.alert}` reads `{self.metric}`, which no rule in alerts/ "
            "reads. That condition has no witness on the delivering plane: the chart's "
            "PrometheusRule is disabled on this cluster, so nothing watches it at all. "
            "Port it into alerts/, or add a keyed waiver with a reason to "
            "scripts/chart_alert_waivers.txt naming the rule that replaced it."
        )


def chart_alerts(text: str) -> dict[str, set[str]]:
    """{chart alert name: metrics its expr reads} for a Go-templated PrometheusRule.

    Parsed textually, not with yaml.safe_load: the file is a Helm template and is not
    valid YAML until rendered, and rendering it would need the chart plus Harbor
    credentials this workflow deliberately does not have."""
    out: dict[str, set[str]] = {}
    lines = text.splitlines()
    current: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _ALERT_LINE.match(line)
        if m is not None:
            current = m.group(2)
            out.setdefault(current, set())
            i += 1
            continue
        m = _EXPR_LINE.match(line)
        if m is None or current is None:
            i += 1
            continue
        indent, inline = m.group(1), m.group(2).strip()
        i += 1
        if inline in ("|", ">", "|-", ">-"):
            # A block scalar: every following line indented deeper than the `expr:` key
            # belongs to it. Stopping at the first line would silently drop half of a
            # multi-term expression and read as "that metric is not used".
            body = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= len(indent):
                    break
                body.append(nxt)
                i += 1
            expr = "\n".join(body)
        else:
            expr = inline
        out[current] |= metric_names(_TEMPLATE_ACTION.sub(" ", expr))
    return out


def ported_metrics(paths: list[str]) -> set[str]:
    """Every metric any rule in alerts/ reads. Reuses check_label_provenance.alert_queries
    so the definition of "a rule's Prometheus query" cannot drift from the checker that
    already owns it (it drops loki queries, which select streams, not metrics)."""
    out: set[str] = set()
    for path in paths:
        for _, expr in alert_queries(path):
            out |= metric_names(expr)
    return out


def load_waivers(path: pathlib.Path) -> dict[tuple[str, str], Waiver]:
    """{(chart alert, metric): Waiver}. Raises ValueError on a malformed or reasonless
    line - see the module docstring on why a bare waiver is not allowed to parse."""
    waivers: dict[tuple[str, str], Waiver] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _WAIVER_LINE.match(line)
        if m is None:
            raise ValueError(
                f"{path}:{lineno}: expected `<ChartAlertName> <metric> # <why>`, got "
                f"{raw!r}. The reason is mandatory: a waiver with no stated reason is an "
                "unexplained coverage gap, and nobody can disagree with it later."
            )
        waivers[(m.group("alert"), m.group("metric"))] = Waiver(m.group("reason"))
    return waivers


def reconcile(
    chart: dict[str, set[str]],
    ported: set[str],
    waivers: dict[tuple[str, str], Waiver],
) -> list[Violation]:
    return [
        Violation(alert, metric)
        for alert in sorted(chart)
        for metric in sorted(chart[alert])
        if metric not in ported and (alert, metric) not in waivers
    ]


def clone_operator(dest: pathlib.Path) -> bool:
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", OPERATOR_REPO, str(dest)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"check_chart_alert_parity: could not clone tatara-operator ({exc}). This is a "
            "HARD failure, not a skip: check_label_provenance.py in this same job already "
            "clones the same repo and already fails closed on it, so a skip here would buy "
            "no availability and only lose signal.",
            file=sys.stderr,
        )
        return False


def _alert_paths() -> list[str]:
    root = pathlib.Path(__file__).resolve().parent.parent
    return sorted(glob.glob(str(root / "alerts" / "*.yaml")))


def main(argv: list[str]) -> int:
    paths = argv[1:] or _alert_paths()
    if not paths:
        print("check_chart_alert_parity: no alert files found", file=sys.stderr)
        return 2
    try:
        waivers = load_waivers(WAIVERS_PATH)
        ported = ported_metrics(paths)
    except (OSError, ValueError) as exc:
        print(f"check_chart_alert_parity: {exc}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="chart-parity-") as tmp:
        dest = pathlib.Path(tmp) / "tatara-operator"
        if not clone_operator(dest):
            return 2
        try:
            chart = chart_alerts((dest / CHART_RULE_PATH).read_text())
        except OSError as exc:
            print(
                f"check_chart_alert_parity: cloned tatara-operator but could not read "
                f"{CHART_RULE_PATH} ({exc}). If the chart stopped shipping a "
                "PrometheusRule, delete this check and its waiver file rather than "
                "letting it pass vacuously.",
                file=sys.stderr,
            )
            return 2

    if not chart:
        print(
            f"check_chart_alert_parity: parsed 0 alerts out of {CHART_RULE_PATH}. That is "
            "a parser break, not a clean chart - failing rather than reporting green on an "
            "empty comparison.",
            file=sys.stderr,
        )
        return 2

    violations = reconcile(chart, ported, waivers)
    if violations:
        print(f"FAIL: {len(violations)} chart alert condition(s) have no witness here:\n")
        for v in violations:
            print(f"  - {v}")
        return 1

    print(
        f"OK: all {len(chart)} tatara-operator chart alerts read metrics that "
        f"{len(paths)} alert file(s) here also read ({len(waivers)} keyed waiver(s))."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
