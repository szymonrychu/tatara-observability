#!/usr/bin/env python3
"""Report which series the alert rules select that Prometheus does not actually hold.

THIS IS THE DIMENSION THE OTHER PROVENANCE CHECKS STRUCTURALLY CANNOT SEE. Every
one of them validates the metric NAME at its PRODUCER: check_metric_provenance.py
asserts the name is in scripts/metrics_allowlist.txt, reconcile_metric_provenance.py
asserts the allowlist matches the producer repos' live Go source, and
check_label_provenance.py does the same for label names. All three were green
throughout tatara-claude-code-wrapper#189, and all three were RIGHT: the names were
allowlisted, the producer emitted them, the labels were declared, and
operator_push_series_dropped_total was 0 on every reason. The series was absent
anyway, because a prometheus.CounterVec child does not exist until its first Inc()
and the wrapper's terminal counters were incremented after the metric pusher had
already shut down and DELETEd the run. Two live alert rules had never been able to
fire, and default_no_data_state: "OK" reported them green the whole time.

So this check asks the only question left: does the series exist? It queries the
cluster's own Prometheus through the Grafana datasource proxy, once per SELECTOR
(not once per metric name), because #189's sharper form is that the family can be
live while the child the rule reads is dark - ccw_turns_total{result="complete"}
existed fleet-wide while ccw_turns_total{result="failed"}, the numerator of
"Wrapper turns erroring", had never existed anywhere. A name-level check would
have called that metric live and reported green on the exact defect it exists
to find.

IT NEVER GATES A PR. Dark metrics are a FLEET condition, not a property of the
change in front of you: a rule can be correct on the day it merges and go dark
three releases later when a producer stops incrementing, and blocking an
unrelated PR on that helps nobody. It reports, on a schedule.

Exit 0 = the check ran (whatever it found). Nonzero = the check COULD NOT RUN
(no credentials, transport failure, a query Prometheus rejected). That asymmetry
is the point: a liveness assertion that silently stops asserting is the same
silent-green failure as the one it is here to catch.
"""

from __future__ import annotations

import glob
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

from check_metric_provenance import _IDENT, _KEYWORDS, _SELECTOR

# The Grafana datasource proxy path for an instant query against a Prometheus uid.
_QUERY_PATH = "/api/datasources/proxy/uid/{uid}/api/v1/query"

_HTTP_TIMEOUT = 30


class CheckError(Exception):
    """The check could not run. Never raised for a metric that is merely dark."""


class Selector:
    """One series selector, as an alert rule literally writes it."""

    def __init__(self, path: str, rule: str, promql: str):
        self.path = path
        self.rule = rule
        self.promql = promql


class Result:
    def __init__(self, selector: Selector, count: int):
        self.selector = selector
        self.count = count

    @property
    def dark(self) -> bool:
        return self.count == 0

    @property
    def rule(self) -> str:
        return self.selector.rule

    @property
    def promql(self) -> str:
        return self.selector.promql

    @property
    def path(self) -> str:
        return self.selector.path


def _bare_names(expr: str) -> list[str]:
    """Metric names selected WITHOUT a label selector, in order of appearance.

    Deliberately does NOT strip histogram suffixes the way
    check_metric_provenance.metric_names does. That check asks about the metric's
    provenance, where the base name is the right unit; this one asks about the
    SERIES, and a histogram's base name has no series of its own - _bucket, _sum
    and _count do.
    """
    # Blank out every `metric{...}` occurrence so what remains is genuinely bare.
    stripped = _SELECTOR.sub("0", expr)
    stripped = re.sub(r"\"[^\"]*\"", "", stripped)
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)
    stripped = re.sub(
        r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)",
        "",
        stripped,
    )
    stripped = re.sub(r"([A-Za-z_][A-Za-z0-9_]*)\s+\(", r"\1(", stripped)
    return [m.group(1) for m in _IDENT.finditer(stripped) if m.group(1) not in _KEYWORDS]


def rule_selectors(rule: dict, path: str = "") -> list[Selector]:
    """Every Prometheus series selector one alert rule reads, deduplicated.

    A selector carrying a Grafana template variable is skipped: `$project` is not
    a literal and querying it would ask Prometheus a question about a series that
    only exists once Grafana has interpolated it.
    """
    name = rule.get("name", "<unnamed>")
    seen: set[str] = set()
    out: list[Selector] = []

    def add(promql: str) -> None:
        if promql in seen:
            return
        seen.add(promql)
        out.append(Selector(path, name, promql))

    for q in rule.get("queries") or []:
        if (q.get("query_type") or "prometheus") != "prometheus":
            continue  # loki queries select log streams, not series
        expr = q.get("expression") or ""
        for metric, body in _SELECTOR.findall(expr):
            if "$" in body:
                continue
            add(f"{metric}{{{body.strip()}}}")
        for metric in _bare_names(expr):
            add(metric)
    return out


def selectors_in_file(path: str) -> list[Selector]:
    data = yaml.safe_load(pathlib.Path(path).read_text())
    if not data or not isinstance(data, dict):
        return []
    out: list[Selector] = []
    for rule in data.get("rules") or []:
        out += rule_selectors(rule, path)
    return out


def classify(selectors: list[Selector], query) -> list[Result]:
    """Ask Prometheus how many series each selector holds. Cached per selector."""
    cache: dict[str, int] = {}
    out: list[Result] = []
    for sel in selectors:
        promql = f"count({sel.promql})"
        if promql not in cache:
            cache[promql] = query(promql)
        out.append(Result(sel, cache[promql]))
    return out


def render(results: list[Result]) -> str:
    """A markdown report. Dark selectors first; they are the reason this runs."""
    dark = [r for r in results if r.dark]
    lines = [
        "## Alert series liveness",
        "",
        f"{len(dark)} of {len(results)} selector(s) read by an alert rule hold NO series.",
        "",
    ]
    if dark:
        lines += [
            "A rule whose selector is dark cannot fire. With "
            '`default_no_data_state: "OK"` it reports green forever, so this is '
            "not a quiet fleet, it is an unwatched one. Either the producer never "
            "increments it (a pre-seed or a lifecycle bug, see "
            "tatara-claude-code-wrapper#189) or the rule outlived the metric.",
            "",
            "| status | selector | rule | file |",
            "|---|---|---|---|",
        ]
        for r in dark:
            lines.append(f"| DARK | `{r.promql}` | {r.rule} | {r.path} |")
        lines.append("")
    else:
        lines += ["Every selector read by an alert rule holds at least one series.", ""]
    return "\n".join(lines)


def run(paths: list[str], query) -> int:
    """Report on paths using query. 0 = the check ran; nonzero = it could not."""
    try:
        selectors: list[Selector] = []
        for path in paths:
            selectors += selectors_in_file(path)
        results = classify(selectors, query)
    except (OSError, yaml.YAMLError, CheckError) as exc:
        print(f"check_series_liveness: cannot run: {exc}", file=sys.stderr)
        return 1
    report = render(results)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 0


def prometheus_query(url: str, api_key: str, uid: str):
    """An instant-query fn against Prometheus through the Grafana datasource proxy."""
    endpoint = url.rstrip("/") + _QUERY_PATH.format(uid=urllib.parse.quote(uid))

    def query(promql: str) -> int:
        req = urllib.request.Request(
            endpoint + "?" + urllib.parse.urlencode({"query": promql}),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise CheckError(f"query {promql!r}: {exc}") from exc
        if payload.get("status") != "success":
            raise CheckError(f"query {promql!r} rejected: {payload.get('error', payload)}")
        # count() over an absent series is an EMPTY vector, not a zero sample.
        # That empty result IS the finding, so it maps to 0 rather than an error.
        result = (payload.get("data") or {}).get("result") or []
        if not result:
            return 0
        return int(float(result[0]["value"][1]))

    return query


def main(argv: list[str]) -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    paths = argv[1:] or sorted(glob.glob(str(root / "alerts" / "*.yaml")))
    if not paths:
        print("check_series_liveness: no alert files found", file=sys.stderr)
        return 1
    url = os.environ.get("GRAFANA_URL") or os.environ.get("TF_VAR_GRAFANA_URL") or ""
    key = (
        os.environ.get("GRAFANA_API_KEY") or os.environ.get("TF_VAR_GRAFANA_API_KEY") or ""
    )
    if not url or not key:
        print(
            "check_series_liveness: GRAFANA_URL and GRAFANA_API_KEY are required; "
            "a check that cannot reach Prometheus must fail, not report a healthy fleet",
            file=sys.stderr,
        )
        return 1
    uid = os.environ.get("GRAFANA_DATASOURCE_UID", "prometheus")
    return run(paths, prometheus_query(url, key, uid))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
