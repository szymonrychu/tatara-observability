#!/usr/bin/env python3
"""Fail CI when a NON-DELIVERING alerting plane alerts on a condition no rule in this
repo watches.

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

That leaves the non-delivering files in a NEW ROLE. They stop being deploy artifacts and
become the SPECIFICATION of conditions the producer thinks are worth alerting on.
`alerts/` is what is actually alerted. Nothing reconciled them, which is exactly how the
situation arose that this replaces: `tatara-operator#635` filed
`TataraAccountUsageFeedDead` as "never written" on the strength of a grep against this
repo, when it had been written, in the plane that delivered nothing. A competent audit of
the wrong plane is indistinguishable from a real gap. This script is the reconciliation.

WHAT IT CHECKS. Every metric an alert on a specification plane reads must be read by at
least one rule in `alerts/`. That is deliberately a METRIC-level check, not a rule-level
one: rule shapes, thresholds and groupings are this repo's business (several ported rules
are better than their originals, and one is worse than the rule that replaced it - see the
waivers), but a metric no rule here reads is a condition with no witness anywhere.

THE THREE SOURCES, and why the name is `alert_plane` rather than `chart_alert`
(tatara-observability#120):

  operator-chart  tatara-operator/charts/tatara-operator/templates/prometheusrule.yaml
  memory-chart    tatara-memory/charts/tatara-memory/templates/prometheusrule.yaml
  operator-go     tatara-operator/internal/memory/monitoring.go (memoryAlertRules)

v1 of this file checked only `operator-chart`, and stated a scope paragraph whose two
reasons for excluding tatara-memory were both false. Recorded here because the correction
is the point of #120: (1) the operator does NOT provision the tatara-memory chart's
PrometheusRule - `monitoring.go:145` `MemoryPrometheusRule` builds an INDEPENDENT Go
re-implementation (`memoryAlertRules`) that shares no bytes with the chart template, which
is precisely how the two drifted by 4 rules unnoticed; and (2) reading it IS a static file
read - `memoryAlertRules` is a plain Go function and its exprs are string literals, as
readable as the chart's. The one path with a measured gap was the one path the guard
declined to look at.

CLONE FAILURE IS A HARD FAILURE HERE, unlike reconcile_metric_provenance.py's neutral skip.
That costs nothing: check_label_provenance.py in this same workflow already clones
tatara-operator and already fails closed on it, so the job cannot pass without a successful
tatara-operator clone either way. A neutral skip would buy no availability and only lose
signal, and "could not see" reported as OK is the precise failure this file exists to end.

WAIVERS ARE PLANE-QUALIFIED, CLASSED, AND MUST CARRY A REASON.
`scripts/alert_plane_waivers.txt` is `<plane>:<AlertName> <metric> # <CLASS>: <why>`, and a
line missing any of those is a parse error, not a pass. The plane qualifier is not
decoration: `MemoryDown` exists on BOTH `memory-chart` and `operator-go`, and an
unqualified key would let one plane's waiver silently blanket the other - the exact class
of silent blanket this file exists to prevent. See that file's header for the two classes
and which plane may use each.

Run: python3 scripts/check_alert_plane_parity.py
Exit 0 = clean, 1 = an unwatched condition, 2 = usage/parse/clone/unresolved-expr error.
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

REPO_URLS = {
    "tatara-operator": "https://github.com/szymonrychu/tatara-operator.git",
    "tatara-memory": "https://github.com/szymonrychu/tatara-memory.git",
}
WAIVERS_PATH = pathlib.Path(__file__).resolve().parent / "alert_plane_waivers.txt"

CHART_PLANES = ("operator-chart", "memory-chart")
GO_PLANES = ("operator-go",)
PLANES = CHART_PLANES + GO_PLANES

# plane -> (repo, path within that repo). Order is the report order. A plane in PLANES
# with no entry here is a hard startup error rather than a KeyError halfway through a
# run: the whole point of this file is that a plane nobody looked at must never read as
# a plane with nothing wrong.
SOURCES = {
    "operator-chart": ("tatara-operator", "charts/tatara-operator/templates/prometheusrule.yaml"),
    "memory-chart": ("tatara-memory", "charts/tatara-memory/templates/prometheusrule.yaml"),
    "operator-go": ("tatara-operator", "internal/memory/monitoring.go"),
}

_ALERT_LINE = re.compile(r"^(\s*)-\s*alert:\s*(\S+)\s*$")
_EXPR_LINE = re.compile(r"^(\s*)expr:\s*(.*)$")
# A Go template action. Stripped before the expression reaches metric_names(), which is a
# PromQL parser and would otherwise read `.Values.prometheusRule.sweepSkipWindow` as a
# bare identifier and invent a metric out of it.
_TEMPLATE_ACTION = re.compile(r"\{\{.*?\}\}", re.S)
_WAIVER_LINE = re.compile(
    r"^(?P<plane>[a-z-]+):(?P<alert>\S+)\s+(?P<metric>\S+)\s*#\s*"
    r"(?P<klass>[A-Z][A-Z-]*):\s*(?P<reason>.*\S)\s*$"
)
_REARM = re.compile(r"\bre-arm:\s*(?P<rearm>.+\S)\s*$")

WAIVER_CLASSES = {
    # class: the planes allowed to use it.
    "SUPERSEDED": set(CHART_PLANES),
    "DORMANT-PRODUCER": set(GO_PLANES),
}


class UnresolvedExpr(Exception):
    """A Go alert expression carries a token the reader cannot account for, in
    metric-name position. Raised rather than skipped: a reader that silently drops the
    rules it cannot parse and reports the rest as clean is this issue one level down."""


class Waiver:
    def __init__(self, klass: str, reason: str, rearm: str | None):
        self.klass = klass
        self.reason = reason
        self.rearm = rearm


class Violation:
    """One specification-plane alert reading a metric no rule in alerts/ reads."""

    def __init__(self, plane: str, alert: str, metric: str):
        self.plane = plane
        self.alert = alert
        self.metric = metric

    def __str__(self) -> str:
        return (
            f"[{self.plane}] alert `{self.alert}` reads `{self.metric}`, which no rule in "
            "alerts/ reads. That condition has no witness on the delivering plane: this "
            "plane is a specification, never loaded by this cluster's Prometheus, so "
            "nothing watches it at all. Port it into alerts/, or add a keyed waiver with "
            "a class and a reason to scripts/alert_plane_waivers.txt."
        )


# ---------------------------------------------------------------------------
# The chart planes: a Go-templated PrometheusRule, read textually.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The Go plane: memoryAlertRules, read as source.
#
# A regex over `intstr.FromString("...")` would read 6 of the 15 rules and print OK.
# The other nine build their expr with fmt.Sprintf, two concatenate a package const, and
# `onPrimary` is a FILE-LOCAL variable holding `cnpg_pg_replication_in_recovery{...}`
# interpolated into three of them - so a literal-only reader misses 7 of the 8 CNPG
# metrics and reports clean. This evaluator resolves package consts, file-local
# assignments and fmt.Sprintf, and hard-fails on anything left over that could be hiding
# a metric name.
#
# Unresolvable operands become MARKERS, and the marker's kind decides whether it can hide
# a metric:
#   N  a numeric verb's operand (%d, %f, ...). The compiler type-checks these to take a
#      number, so they can never introduce a metric name - a threshold, an instance
#      count. Substituted with 0 in both passes.
#   S  a string verb's operand (%s, %q, %v) that resolved to nothing at all.
#   U  an unresolved identifier spliced by `+` concatenation.
# S and U CAN hide a metric name, so pass B substitutes them with a sentinel and any
# sentinel surviving metric_names() - which strips `{...}` selector bodies, so an operand
# that only ever lands inside a selector is correctly invisible - is a hard error naming
# the identifier.
# ---------------------------------------------------------------------------

_GO_ALERT = re.compile(r'Alert:\s*"([A-Za-z][A-Za-z0-9_]*)"')
_GO_ASSIGN = re.compile(r"^[ \t]*(\w+)\s*(?::=|=)\s*(.+)$", re.M)
_GO_STRING = re.compile(r"`[^`]*`|\"(?:[^\"\\]|\\.)*\"")
_GO_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_VERB = re.compile(r"%(?:\[(\d+)\])?[-+ #0]*[\d.]*([a-zA-Z%])")
_MARKER = re.compile("\x00([NSU])(\\d+)\x00")
_NUMERIC_VERBS = set("bcdeEfFgGoOpqxXUt")
_STRING_VERBS = set("svw")


def _unquote(lit: str) -> str:
    if lit.startswith("`"):
        return lit[1:-1]
    return lit[1:-1].replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` at nesting depth 0, ignoring separators inside strings."""
    parts, buf, depth, i = [], [], 0, 0
    while i < len(text):
        ch = text[i]
        if ch in "`\"":
            m = _GO_STRING.match(text, i)
            if m is not None:
                buf.append(m.group(0))
                i = m.end()
                continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _balanced(text: str, start: int) -> tuple[str, int]:
    """The text inside the parenthesised group opening at `start`, and the index just
    past its close. Strings are skipped so a `)` inside a literal does not close it."""
    depth, i, out = 0, start, []
    while i < len(text):
        ch = text[i]
        if ch in "`\"":
            m = _GO_STRING.match(text, i)
            if m is not None:
                out.append(m.group(0))
                i = m.end()
                continue
        if ch == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise UnresolvedExpr(f"unbalanced parentheses from offset {start}")


def _go_env(src: str) -> dict[str, str]:
    """{identifier: its Go right-hand side} for package consts and file-local
    assignments. The value is raw Go source; _eval resolves it on demand, so a const
    defined in terms of another const still resolves."""
    env: dict[str, str] = {}
    for m in _GO_ASSIGN.finditer(src):
        name, rhs = m.group(1), m.group(2).strip()
        rhs = rhs.rstrip(",")
        if name in env:
            continue
        env[name] = rhs
    return env


class _Evaluator:
    def __init__(self, env: dict[str, str]):
        self.env = env
        self.markers: list[str] = []

    def mark(self, kind: str, what: str) -> str:
        self.markers.append(what)
        return f"\x00{kind}{len(self.markers) - 1}\x00"

    def eval(self, text: str, seen: frozenset[str] = frozenset()) -> str:
        text = text.strip()
        if not text:
            return ""
        if text.startswith("fmt.Sprintf("):
            inner, _ = _balanced(text, text.index("("))
            args = [a.strip() for a in _split_top(inner, ",") if a.strip()]
            if not args:
                return self.mark("U", text)
            fmt = self.eval(args[0], seen)
            if _MARKER.search(fmt):
                # The format string itself is unknown, so nothing about the shape of
                # this expression is known. Fail closed rather than guess.
                return self.mark("S", args[0])
            return self._format(fmt, [self.eval(a, seen) for a in args[1:]], args[1:])
        parts = _split_top(text, "+")
        if len(parts) > 1:
            return "".join(self.eval(p, seen) for p in parts)
        if _GO_STRING.fullmatch(text):
            return _unquote(text)
        if _GO_IDENT.match(text) and text in self.env and text not in seen:
            return self.eval(self.env[text], seen | {text})
        return self.mark("U", text)

    def _format(self, fmt: str, values: list[str], sources: list[str]) -> str:
        out, pos, auto = [], 0, 0
        for m in _VERB.finditer(fmt):
            out.append(fmt[pos : m.start()])
            pos = m.end()
            index, verb = m.group(1), m.group(2)
            if verb == "%":
                out.append("%")
                continue
            n = int(index) - 1 if index else auto
            if not index:
                auto += 1
            value = values[n] if 0 <= n < len(values) else None
            source = sources[n] if 0 <= n < len(sources) else fmt
            if value is None or _MARKER.fullmatch(value):
                kind = "N" if verb in _NUMERIC_VERBS and verb not in _STRING_VERBS else "S"
                if verb == "q":
                    kind = "S"
                out.append(self.mark(kind, source))
                continue
            out.append(f'"{value}"' if verb == "q" else value)
        out.append(fmt[pos:])
        return "".join(out)


def go_alerts(src: str) -> dict[str, set[str]]:
    """{alert name: metrics its expr reads} for tatara-operator's memoryAlertRules.

    Raises UnresolvedExpr when a token the evaluator could not resolve lands in
    metric-name position."""
    env = _go_env(src)
    out: dict[str, set[str]] = {}
    matches = list(_GO_ALERT.finditer(src))
    for i, m in enumerate(matches):
        alert = m.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(src)
        region = src[m.end() : end]
        call = region.find("intstr.FromString(")
        if call < 0:
            out.setdefault(alert, set())
            continue
        inner, _ = _balanced(region, region.index("(", call))
        ev = _Evaluator(env)
        # rstrip the trailing comma of a multi-line Go call before evaluating.
        text = ev.eval(inner.strip().rstrip(","))
        neutral = _MARKER.sub("0", text)
        probed = _MARKER.sub(
            lambda mk: "0" if mk.group(1) == "N" else f"tatara_unresolved_{mk.group(2)}",
            text,
        )
        leaked = sorted(
            ev.markers[int(name.split("_")[-1])]
            for name in metric_names(probed)
            if name.startswith("tatara_unresolved_")
        )
        if leaked:
            raise UnresolvedExpr(
                f"{alert}: the expression reads {', '.join(repr(x) for x in leaked)}, "
                "which this reader cannot resolve to literal text, in metric-name "
                "position. It may be a metric with no witness in alerts/, so reporting "
                "the other rules as clean would be exactly the failure this check "
                "exists to catch. Resolve it (a package const or a file-local string "
                "assignment both resolve), or move the rule set into a data file this "
                "guard can read."
            )
        out.setdefault(alert, set())
        out[alert] |= metric_names(neutral)
    return out


# ---------------------------------------------------------------------------
# The delivering plane, the waivers, and the reconciliation.
# ---------------------------------------------------------------------------


def ported_metrics(paths: list[str]) -> set[str]:
    """Every metric any rule in alerts/ reads. Reuses check_label_provenance.alert_queries
    so the definition of "a rule's Prometheus query" cannot drift from the checker that
    already owns it (it drops loki queries, which select streams, not metrics)."""
    out: set[str] = set()
    for path in paths:
        for _, expr in alert_queries(path):
            out |= metric_names(expr)
    return out


def load_waivers(path: pathlib.Path) -> dict[tuple[str, str, str], Waiver]:
    """{(plane, alert, metric): Waiver}. Raises ValueError on a malformed, unqualified,
    unclassed or reasonless line - see the module docstring and the waiver file's own
    header on why none of those is allowed to parse."""
    waivers: dict[tuple[str, str, str], Waiver] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _WAIVER_LINE.match(line)
        if m is None:
            raise ValueError(
                f"{path}:{lineno}: expected `<plane>:<AlertName> <metric> # <CLASS>: "
                f"<why>`, got {raw!r}. The plane qualifier, the class and the reason are "
                "all mandatory: an unqualified key lets one plane's waiver blanket "
                "another's identically-named alert, and a waiver with no stated reason "
                "is an unexplained coverage gap nobody can disagree with later."
            )
        plane, klass = m.group("plane"), m.group("klass")
        if plane not in PLANES:
            raise ValueError(
                f"{path}:{lineno}: unknown plane {plane!r}. Known planes: "
                f"{', '.join(PLANES)}."
            )
        allowed = WAIVER_CLASSES.get(klass)
        if allowed is None:
            raise ValueError(
                f"{path}:{lineno}: unknown waiver class {klass!r}. Known classes: "
                f"{', '.join(sorted(WAIVER_CLASSES))}."
            )
        if plane not in allowed:
            raise ValueError(
                f"{path}:{lineno}: class {klass} is not available to plane {plane!r} "
                f"(allowed there: {', '.join(sorted(allowed)) or 'none'}). See the "
                "waiver file's header for why each class is restricted."
            )
        rearm = None
        if klass == "DORMANT-PRODUCER":
            r = _REARM.search(m.group("reason"))
            if r is None:
                raise ValueError(
                    f"{path}:{lineno}: a DORMANT-PRODUCER waiver must carry a "
                    "`re-arm: <precondition>` clause naming what has to become true "
                    "before the condition needs a witness again. Without it the waiver "
                    'is indistinguishable from "we decided not to", which this file '
                    "forbids."
                )
            rearm = r.group("rearm")
        waivers[(plane, m.group("alert"), m.group("metric"))] = Waiver(
            klass, m.group("reason"), rearm
        )
    return waivers


def reconcile(
    plane: str,
    alerts: dict[str, set[str]],
    ported: set[str],
    waivers: dict[tuple[str, str, str], Waiver],
) -> list[Violation]:
    return [
        Violation(plane, alert, metric)
        for alert in sorted(alerts)
        for metric in sorted(alerts[alert])
        if metric not in ported and (plane, alert, metric) not in waivers
    ]


def clone(repo: str, url: str, dest: pathlib.Path) -> bool:
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"check_alert_plane_parity: could not clone {repo} ({exc}). This is a HARD "
            "failure, not a skip: check_label_provenance.py in this same job already "
            "clones tatara-operator and already fails closed on it, so a skip here would "
            "buy no availability and only lose signal.",
            file=sys.stderr,
        )
        return False


def _alert_paths() -> list[str]:
    root = pathlib.Path(__file__).resolve().parent.parent
    return sorted(glob.glob(str(root / "alerts" / "*.yaml")))


def _read_plane(plane: str, path: pathlib.Path) -> dict[str, set[str]]:
    text = path.read_text()
    return go_alerts(text) if plane in GO_PLANES else chart_alerts(text)


def unsourced_planes() -> list[str]:
    """Planes declared in PLANES that SOURCES or REPO_URLS cannot actually reach. Empty
    on a consistent table; anything else means a plane would be skipped or would crash
    mid-run, and a plane nobody looked at must never read as a plane with nothing
    wrong."""
    return sorted(
        plane
        for plane in PLANES
        if plane not in SOURCES or SOURCES[plane][0] not in REPO_URLS
    )


def main(argv: list[str]) -> int:
    paths = argv[1:] or _alert_paths()
    if not paths:
        print("check_alert_plane_parity: no alert files found", file=sys.stderr)
        return 2
    unsourced = unsourced_planes()
    if unsourced:
        print(
            f"check_alert_plane_parity: {', '.join(unsourced)} declared in PLANES but "
            "not reachable through SOURCES/REPO_URLS. Failing at startup rather than "
            "silently checking fewer planes than the name promises.",
            file=sys.stderr,
        )
        return 2
    try:
        waivers = load_waivers(WAIVERS_PATH)
        ported = ported_metrics(paths)
    except (OSError, ValueError) as exc:
        print(f"check_alert_plane_parity: {exc}", file=sys.stderr)
        return 2

    planes: dict[str, dict[str, set[str]]] = {}
    with tempfile.TemporaryDirectory(prefix="alert-plane-parity-") as tmp:
        roots: dict[str, pathlib.Path] = {}
        for repo, url in REPO_URLS.items():
            dest = pathlib.Path(tmp) / repo
            if not clone(repo, url, dest):
                return 2
            roots[repo] = dest
        for plane in PLANES:
            repo, rel = SOURCES[plane]
            try:
                planes[plane] = _read_plane(plane, roots[repo] / rel)
            except OSError as exc:
                print(
                    f"check_alert_plane_parity: cloned {repo} but could not read {rel} "
                    f"({exc}). If that plane stopped shipping alerts, delete it from "
                    "PLANES and drop its waivers rather than letting it pass vacuously.",
                    file=sys.stderr,
                )
                return 2
            except UnresolvedExpr as exc:
                print(f"check_alert_plane_parity: [{plane}] {exc}", file=sys.stderr)
                return 2

    empty = [plane for plane, alerts in planes.items() if not alerts]
    if empty:
        print(
            f"check_alert_plane_parity: parsed 0 alerts out of {', '.join(empty)}. That "
            "is a parser break, not a clean plane - failing rather than reporting green "
            "on an empty comparison.",
            file=sys.stderr,
        )
        return 2

    violations = [v for plane in PLANES for v in reconcile(plane, planes[plane], ported, waivers)]
    if violations:
        print(f"FAIL: {len(violations)} specification condition(s) have no witness here:\n")
        for v in violations:
            print(f"  - {v}")
        return 1

    total = sum(len(a) for a in planes.values())
    print(
        f"OK: all {total} alerts across {len(PLANES)} specification plane(s) "
        f"({', '.join(PLANES)}) read metrics that {len(paths)} alert file(s) here also "
        f"read ({len(waivers)} keyed waiver(s))."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
