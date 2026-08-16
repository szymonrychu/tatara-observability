#!/usr/bin/env python3
"""Lint tatara alert rules for the benign/transient classification convention
PLUS the structural alert-shape checks documented in CONVENTIONS.md.

Enforces the "filter-or-justify" rule from CONVENTIONS.md: any alert rule whose
PromQL selects a server-error status on an *http_requests_total metric family
MUST do one of:

  1. exclude probe routes in the selector itself (consumer-side filter), e.g.
     route!~"/readyz|/healthz|/metrics"; or
  2. carry a non-empty `tatara_probe_exclusion` annotation explaining where the
     probes are kept out of the series (producer-side exclusion, or a documented
     known gap with a follow-up).

This catches the recurring false-positive class where readiness/liveness probe
5xx responses (a DB blip or pod boot returns 503 on /readyz) are counted as real
HTTP errors and page an on-call incident. See CONVENTIONS.md for the full
convention and the four canonical patterns.

Scope: deterministic, zero false-failures. It only inspects rules that select a
server-error status on an *http_requests_total family; everything else (the
operator result=error taxonomy, latency rules, kube-state rules) is ignored.

Exit 0 = clean, exit 1 = violations found, exit 2 = usage/parse error.
"""

from __future__ import annotations

import glob
import math
import pathlib
import re
import sys

import yaml

# A metric whose name ends in http_requests_total, with its label selector. The
# optional prefix matches families like ccw_http_requests_total; the lookbehind
# keeps us from starting in the middle of a longer identifier.
_METRIC_SELECTOR = re.compile(
    r"(?<![A-Za-z0-9_:])(?P<metric>[A-Za-z0-9_:]*http_requests_total)\s*\{(?P<sel>[^}]*)\}"
)

# A status/status_code/code label matched (=~ or =) against some value.
_STATUS_MATCHER = re.compile(r"(?:status_code|status|code)\s*(?:=~|=)\s*\"(?P<val>[^\"]*)\"")

# A value that selects a server (5xx) error: 5.. / 5xx / literal 5\d\d / a 5xx code.
_SERVER_ERROR_VALUE = re.compile(r"5\.\.|5xx|5\\d\\d|\b5\d\d\b")

# Named HTTP 5xx statuses (Go net/http http.StatusText form) some services emit
# as the `status` label value instead of a numeric code.
_SERVER_ERROR_NAMES = (
    "Internal Server Error",
    "Not Implemented",
    "Bad Gateway",
    "Service Unavailable",
    "Gateway Timeout",
    "HTTP Version Not Supported",
    "Variant Also Negotiates",
    "Insufficient Storage",
    "Loop Detected",
    "Not Extended",
    "Network Authentication Required",
)

# A negative matcher on a route-ish label that excludes a probe path. This is the
# consumer-side "filter" half of filter-or-justify.
_PROBE_FILTER = re.compile(
    r"(?:route|path|handler|uri|url|endpoint)\s*(?:!~|!=)\s*\"[^\"]*(?:readyz|healthz)[^\"]*\""
)

ANNOTATION_KEY = "tatara_probe_exclusion"


class Violation:
    """One rule (or one file-level default) that breaks an alert-shape convention.

    `rule` is the rule name, or a "<file ...>" placeholder for a file-scope
    finding. `message` completes the sentence 'rule "<name>" ...'.
    """

    def __init__(self, path: str, rule: str, message: str):
        self.path = path
        self.rule = rule
        self.message = message

    def __str__(self) -> str:
        return f'{self.path}: rule "{self.rule}" {self.message}'


def _selects_server_error(selector: str) -> bool:
    for m in _STATUS_MATCHER.finditer(selector):
        val = m.group("val")
        if _SERVER_ERROR_VALUE.search(val):
            return True
        if any(name in val for name in _SERVER_ERROR_NAMES):
            return True
    return False


def _error_http_metric(joined_exprs: str) -> str | None:
    """Return the first *http_requests_total metric that selects a server error."""
    for m in _METRIC_SELECTOR.finditer(joined_exprs):
        if _selects_server_error(m.group("sel")):
            return m.group("metric")
    return None


def lint_rule(path: str, rule: dict) -> Violation | None:
    joined = _joined_expressions(rule)
    metric = _error_http_metric(joined)
    if metric is None:
        return None  # not an http error-ratio rule; out of scope for this lint
    if _PROBE_FILTER.search(joined):
        return None  # consumer-side filter present
    annotations = rule.get("annotations") or {}
    if str(annotations.get(ANNOTATION_KEY, "")).strip():
        return None  # justified
    return Violation(
        path,
        rule.get("name", "<unnamed>"),
        f"selects server errors on `{metric}` but neither excludes probe routes in "
        f"the selector (e.g. route!~\"/readyz|/healthz|/metrics\") nor sets a "
        f"non-empty `{ANNOTATION_KEY}` annotation. See CONVENTIONS.md.",
    )


def _joined_expressions(rule: dict) -> str:
    queries = rule.get("queries") or []
    return "\n".join(q.get("expression", "") or "" for q in queries)


# --- Check 1: fabricated-zero deadman on a foreign exporter's metric ---------
#
# `or vector(0)` (and its `or on() vector(0)` form) substitutes a literal zero
# when the vector is empty. Paired with a `<` threshold on a metric produced by a
# DIFFERENT exporter than the system being alerted on, that turns "the exporter is
# unscrapeable" into "the alerted system is down" - tatara-observability#67, where
# a kube-state-metrics gap paged that the operator was down while
# up{job="tatara-operator"}=1 throughout. Use absent()/absent_over_time(), an
# independent cross-exporter gate, or noDataState instead.
_OR_VECTOR_ZERO = re.compile(r"\bor\b(?:\s+on\s*\([^)]*\))?\s+vector\s*\(\s*0\s*\)")
_FOREIGN_METRIC = re.compile(r"(?<![A-Za-z0-9_:])kube_[a-z0-9_]+")
DEADMAN_ANNOTATION_KEY = "tatara_absence_fires"


def lint_fabricated_zero(path: str, rule: dict) -> Violation | None:
    joined = _joined_expressions(rule)
    if not _OR_VECTOR_ZERO.search(joined):
        return None
    if str(rule.get("math_operator", ">")).strip() not in ("<", "<="):
        return None
    m = _FOREIGN_METRIC.search(joined)
    if m is None:
        return None
    annotations = rule.get("annotations") or {}
    if str(annotations.get(DEADMAN_ANNOTATION_KEY, "")).strip():
        return None
    return Violation(
        path,
        rule.get("name", "<unnamed>"),
        f"pairs `or vector(0)` with a `{rule.get('math_operator')}` threshold on "
        f"`{m.group(0)}`, a metric from a different exporter than the system this "
        f"rule alerts on: an exporter gap fabricates a zero and pages for the wrong "
        f"system. Gate the rule on that exporter being up, use absent(), or set a "
        f"non-empty `{DEADMAN_ANNOTATION_KEY}` annotation. See CONVENTIONS.md.",
    )


# --- Check 2: idle-NaN quantile guard ---------------------------------------
#
# histogram_quantile over a bucket set with no samples yields NaN, and an idle
# service is not a slow service (CONVENTIONS.md section 1). The compliant shape,
# and the reference example, is alerts/tatara-operator.yaml's
# "Operator turn submit p95 latency high":
#   histogram_quantile(0.95, ...) and on() (sum(rate(<metric>_count[w])) > 0)
#
# BEING IDLE-GUARDED IS NOT ENOUGH ON ITS OWN. Until #111 that same rule was the
# reference example while thresholding at `> 30` over a histogram topping out at
# 25.6 - correctly guarded against the idle NaN and still unable to fire on any
# input, ever. A compliant quantile rule must clear Check 4 below as well; the two
# checks close different halves of the same silent-green failure.
#
# The guard must be tied to the SAME metric family as the histogrammed
# `<family>_bucket` selector inside the histogram_quantile( call - a `_count`
# reference on an unrelated family elsewhere in the expression does not prove
# that family's own idle NaN is guarded. Each histogram_quantile( call in an
# expression is checked (and must be guarded) independently.
_HISTOGRAM_QUANTILE = re.compile(r"\bhistogram_quantile\s*\(")
_BUCKET_FAMILY = re.compile(r"(\w+)_bucket\b")
QUANTILE_ANNOTATION_KEY = "tatara_idle_quantile"


def _call_arguments(text: str, open_paren_index: int) -> str:
    """Return the text between the parens of a call whose '(' sits at
    open_paren_index, tracking nesting so an inner `sum(...)`/`rate(...)`
    doesn't end the scan early."""
    depth = 0
    for i in range(open_paren_index, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_index + 1 : i]
    return text[open_paren_index + 1 :]  # unterminated call; use what's there


def _quantile_call_families(expr: str) -> list[str | None]:
    """One entry per histogram_quantile( call in expr, in order: the
    <family> in that call's own <family>_bucket selector, or None if the
    call's arguments carry no recognisable _bucket selector."""
    families = []
    for m in _HISTOGRAM_QUANTILE.finditer(expr):
        args = _call_arguments(expr, m.end() - 1)
        fm = _BUCKET_FAMILY.search(args)
        families.append(fm.group(1) if fm else None)
    return families


def lint_idle_quantile(path: str, rule: dict) -> Violation | None:
    joined = _joined_expressions(rule)
    families = _quantile_call_families(joined)
    if not families:
        return None
    annotations = rule.get("annotations") or {}
    justified = str(annotations.get(QUANTILE_ANNOTATION_KEY, "")).strip()
    for family in families:
        if family is None:
            if justified:
                continue
            return Violation(
                path,
                rule.get("name", "<unnamed>"),
                "uses histogram_quantile() but no `<metric>_bucket` selector could be "
                "identified in its own arguments, so no same-family idle guard could "
                "be verified: an empty bucket set yields NaN and an idle service is "
                "not a slow service. Use a recognisable `<metric>_bucket` selector, "
                f"or set a non-empty `{QUANTILE_ANNOTATION_KEY}` annotation. "
                "See CONVENTIONS.md.",
            )
        # (?![.\d]) keeps a fractional threshold like `> 0.2` from matching as an idle
        # guard: a ratio alert's own condition (e.g.
        # histogram_quantile(...) / sum(rate(x_count[5m])) > 0.2) is not an idle guard,
        # it is the alert's threshold, and `> 0` alone would match its leading digits.
        guard = re.compile(rf"{re.escape(family)}_count\b.*?>\s*0(?![.\d])", re.S)
        if guard.search(joined):
            continue
        if justified:
            continue
        return Violation(
            path,
            rule.get("name", "<unnamed>"),
            f"uses histogram_quantile() over `{family}_bucket` with no matching "
            f"`{family}_count ... > 0` idle guard: an empty bucket set yields NaN "
            "and an idle service is not a slow service. Add "
            f"`and on() (sum(rate({family}_count[w])) > 0)` (see "
            "alerts/tatara-operator.yaml's \"Operator turn submit p95 latency high\") "
            f"or set a non-empty `{QUANTILE_ANNOTATION_KEY}` annotation. See CONVENTIONS.md.",
        )
    return None


# --- Check 4: threshold outside the histogram's representable range ----------
#
# Classic histogram_quantile() returns AT MOST the top finite bucket bound: a
# quantile landing in the +Inf bucket yields that bound, never anything above it.
# A rule thresholding `> ceiling` therefore cannot fire on any input, ever - and
# because every alert file sets default_no_data_state: "OK" and grafana.tf sets
# default_exec_err_state = "OK", it does not go stale and does not error either.
# It reports OK forever, which is the same silent-green class as an alert on a
# metric nobody emits, one level further down (tatara-observability#111: two p95
# rules at `> 30` over 25.6s and 10s ceilings).
#
# THE REACHABLE SET IS [0, top finite bound], NOT [lowest bound, top bound].
# Prometheus's bucketQuantile interpolates the LOWEST bucket from 0, not from its
# own lower bound (`bucketStart` stays 0 when b == 0), and histogram_quantile(0, ...)
# returns exactly 0. So a `<` rule is inert only at threshold <= 0 - it is NOT
# inert below the smallest bucket bound, which is what tatara-observability#111's
# pre-mortem 1 assumed.
#
# decimal_points is applied FIRST: modules/grafana_alert/main.tf:207 inserts a
# `round($C * 10^d) / 10^d` reduce step ahead of the threshold compare, so the
# ceiling the compare actually sees is the rounded one. Rounding only ever widens
# the ceiling upward (round(25.6) at 0dp is 26), so ignoring it would reject a
# legal `> 25.9 @ 0dp` rule. Grafana's round() is Go math.Round (half away from
# zero), which is NOT Python's banker's rounding - hence the explicit floor(x+0.5).
#
# A family with no entry in histogram_bounds.txt is a hard FAIL, not a skip: this
# check exists to make the NEXT quantile rule safe, and a silently-skipped unknown
# family is the same bypass the check is here to close.
_BOUNDS_ENTRY = re.compile(r"^(?P<family>[a-z][a-z0-9_]*)\s+(?P<bound>[-+0-9.eE]+)$")
HISTOGRAM_RANGE_ANNOTATION_KEY = "tatara_histogram_range"


def _bounds_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "histogram_bounds.txt"


def load_histogram_bounds(path: pathlib.Path | None = None) -> dict[str, float]:
    """{metric family: top finite bucket bound} from histogram_bounds.txt.

    Comment and blank lines (including the `# --- <section> ---` headers, which
    only reconcile_metric_provenance.py routes on) are skipped.
    """
    out: dict[str, float] = {}
    for line in (path or _bounds_path()).read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _BOUNDS_ENTRY.match(stripped)
        if m:
            out[m.group("family")] = float(m.group("bound"))
    return out


def _rounded(value: float, decimal_points: int) -> float:
    """value through main.tf's `round($C * 10^d) / 10^d` reduce step, with Go's
    math.Round semantics (half away from zero) rather than Python's banker's."""
    scale = 10**decimal_points
    scaled = value * scale
    return (
        math.floor(scaled + 0.5) / scale
        if scaled >= 0
        else -math.floor(-scaled + 0.5) / scale
    )


def _threshold_reachable(ceiling: float, threshold: float, operator: str) -> bool:
    """Can the post-reduce quantile, whose range is [0, ceiling], satisfy
    `value <operator> threshold` for any input?"""
    if operator == ">":
        return ceiling > threshold
    if operator == ">=":
        return ceiling >= threshold
    if operator == "<":
        return threshold > 0
    if operator == "<=":
        return threshold >= 0
    return True  # an operator this check does not model is not judged


def lint_histogram_range(
    path: str, rule: dict, bounds: dict[str, float]
) -> Violation | None:
    joined = _joined_expressions(rule)
    families = _quantile_call_families(joined)
    if not families:
        return None
    annotations = rule.get("annotations") or {}
    if str(annotations.get(HISTOGRAM_RANGE_ANNOTATION_KEY, "")).strip():
        return None
    name = rule.get("name", "<unnamed>")
    operator = str(rule.get("math_operator", ">")).strip()
    try:
        threshold = float(rule.get("threshold"))
        decimal_points = int(rule.get("decimal_points", 2))
    except (TypeError, ValueError):
        return None  # check_alert_schema.py owns malformed keys
    for family in families:
        if family is None:
            return Violation(
                path,
                name,
                "uses histogram_quantile() with no identifiable `<metric>_bucket` "
                "selector in its own arguments, so the range its threshold is "
                "compared against cannot be verified: a threshold outside the "
                "reachable range makes the rule structurally inert and it reports "
                "OK forever. Use a recognisable `<metric>_bucket` selector, or set "
                f"a non-empty `{HISTOGRAM_RANGE_ANNOTATION_KEY}` annotation. "
                "See CONVENTIONS.md.",
            )
        if family not in bounds:
            return Violation(
                path,
                name,
                f"runs histogram_quantile() over `{family}_bucket`, which has no "
                "bucket-ceiling entry in `scripts/histogram_bounds.txt`, so its "
                "threshold cannot be range-checked. Add `<family> <top finite "
                "bucket bound>` there (reconcile_metric_provenance.py validates it "
                "against the producer's Go source), or set a non-empty "
                f"`{HISTOGRAM_RANGE_ANNOTATION_KEY}` annotation. See CONVENTIONS.md.",
            )
        ceiling = _rounded(bounds[family], decimal_points)
        if _threshold_reachable(ceiling, threshold, operator):
            continue
        return Violation(
            path,
            name,
            f"compares a histogram_quantile() over `{family}_bucket` against "
            f"`{operator} {threshold:g}`, which that quantile can never satisfy: "
            f"classic histogram_quantile returns at most the top finite bucket "
            f"bound, {bounds[family]:g}"
            + (
                f" ({ceiling:g} after the decimal_points: {decimal_points} reduce step)"
                if ceiling != bounds[family]
                else ""
            )
            + ", and interpolates the lowest bucket from 0, so its range is "
            f"[0, {ceiling:g}]. The rule is structurally inert - it cannot fire on "
            "any input, and default_no_data_state: \"OK\" means it never goes stale "
            "either. Move the threshold inside the range, widen the producer's "
            f"buckets, or set a non-empty `{HISTOGRAM_RANGE_ANNOTATION_KEY}` "
            "annotation. See CONVENTIONS.md.",
        )
    return None


# --- Check 3: self-firing rule ----------------------------------------------
#
# exec_err_state: Alerting makes a rule page on its OWN query failure - a timed-out
# or malformed query is reported as the condition the rule watches for
# (tatara-observability#63: a ~9.6s LogQL pipeline paging on its own timeout).
# Grafana changed this same default from Alerting to Error in 9.2.0 (PR #55345,
# issue #46398). "An absent series means the system is broken" is an argument for
# noDataState, which is a DIFFERENT knob.
#
# Alerting is justified by a non-empty tatara_exec_err_justification key AT THE
# SAME SCOPE it is set: a rule-level annotation for a rule-level setting, a
# top-level file key for a file-level default. Terraform's object-type conversion
# silently drops the top-level key, so it never reaches Grafana.
EXEC_ERR_ANNOTATION_KEY = "tatara_exec_err_justification"


def lint_file_exec_err_state(path: str, data: dict) -> Violation | None:
    if str(data.get("default_exec_err_state", "")).strip() != "Alerting":
        return None
    if str(data.get(EXEC_ERR_ANNOTATION_KEY, "")).strip():
        return None
    return Violation(
        path,
        "<file default_exec_err_state>",
        "sets a file-level `default_exec_err_state: Alerting`, so every rule in "
        "the file pages on its own query failure, with no non-empty top-level "
        f"`{EXEC_ERR_ANNOTATION_KEY}` key saying why. Use \"Error\" unless a query "
        "failure genuinely IS the condition. See CONVENTIONS.md.",
    )


def lint_rule_exec_err_state(path: str, rule: dict) -> Violation | None:
    own = rule.get("exec_err_state")
    if own is None:
        # Inherits the file default. If that default is Alerting and unjustified,
        # lint_file_exec_err_state reports it ONCE at file scope; do not repeat it
        # per rule.
        return None
    if str(own).strip() != "Alerting":
        return None
    annotations = rule.get("annotations") or {}
    if str(annotations.get(EXEC_ERR_ANNOTATION_KEY, "")).strip():
        return None
    return Violation(
        path,
        rule.get("name", "<unnamed>"),
        "sets `exec_err_state: Alerting`, so it pages on its own query failure, "
        f"with no non-empty `{EXEC_ERR_ANNOTATION_KEY}` annotation saying why. Use "
        "\"Error\" unless a query failure genuinely IS the condition; noDataState "
        "is the knob for \"an absent series is the failure\". See CONVENTIONS.md.",
    )


def lint_file(path: str, bounds: dict[str, float] | None = None) -> list[Violation]:
    if bounds is None:
        bounds = load_histogram_bounds()
    data = yaml.safe_load(pathlib.Path(path).read_text())
    if not data or not isinstance(data, dict):
        return []
    out = []
    v = lint_file_exec_err_state(path, data)
    if v is not None:
        out.append(v)
    for rule in data.get("rules") or []:
        for check in (lint_rule, lint_fabricated_zero, lint_idle_quantile):
            v = check(path, rule)
            if v is not None:
                out.append(v)
        v = lint_histogram_range(path, rule, bounds)
        if v is not None:
            out.append(v)
        v = lint_rule_exec_err_state(path, rule)
        if v is not None:
            out.append(v)
    return out


def lint_paths(paths: list[str]) -> list[Violation]:
    bounds = load_histogram_bounds()
    out = []
    for p in paths:
        out.extend(lint_file(p, bounds))
    return out


def _default_paths() -> list[str]:
    root = pathlib.Path(__file__).resolve().parent.parent
    return sorted(glob.glob(str(root / "alerts" / "*.yaml")))


def main(argv: list[str]) -> int:
    paths = argv[1:] or _default_paths()
    if not paths:
        print("lint_alert_rules: no alert files found", file=sys.stderr)
        return 2
    try:
        violations = lint_paths(paths)
    except (OSError, yaml.YAMLError) as exc:
        print(f"lint_alert_rules: {exc}", file=sys.stderr)
        return 2
    if violations:
        print(f"FAIL: {len(violations)} alert rule(s) violate the tatara alert conventions:\n")
        for v in violations:
            print(f"  - {v}")
        print("\nEach message names its own fix. See CONVENTIONS.md.")
        return 1
    print(f"OK: {len(paths)} alert file(s) pass the tatara alert conventions lint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
