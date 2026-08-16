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

It also lints dashboards/*.json - see Check 5. That is not a scope slip: the
unreachable quantile threshold Check 4 exists for was live on both surfaces at once,
and a panel's red threshold band is the same comparison against the same histogram.
A .json path is routed to the dashboard checks and a .yaml path to the rule checks.

Exit 0 = clean, exit 1 = violations found, exit 2 = usage/parse error.
"""

from __future__ import annotations

import glob
import json
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
# THE REACHABLE SET IS [q * lowest finite bound, top finite bound]. Prometheus's
# bucketQuantile does interpolate the lowest bucket from 0 rather than from its own
# lower bound (`bucketStart` stays 0 when b == 0), but that is not the whole story:
# on that branch it returns `ub0 * (rank/count)` where `count` is bucket 0's own
# count and `rank` is `q * observations`. Selecting b == 0 requires
# `count >= rank`, and `count <= observations`, so `rank/count` is bounded in
# [q, 1] and the result is bounded in [q*ub0, ub0]. The infimum q*ub0 is ATTAINED,
# when every observation lands in the lowest bucket. So a p95 over
# ExponentialBuckets(0.05, 2, 10) can never return below 0.0475, and `< 0.01` on it
# is exactly as inert as `> 30` - which is why this check parses the quantile
# argument out of each call rather than assuming a floor of 0.
#
# (Zero is reachable only at q == 0, and a histogram whose lowest bound is <= 0 is
# special-cased by bucketQuantile to return that bound directly, so the floor is
# the bound itself rather than q times it.)
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
_BOUNDS_ENTRY = re.compile(
    r"^(?P<family>[a-z][a-z0-9_]*)\s+(?P<low>\S+)\s+(?P<high>\S+)$"
)
HISTOGRAM_RANGE_ANNOTATION_KEY = "tatara_histogram_range"


def _bounds_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "histogram_bounds.txt"


def load_histogram_bounds(
    path: pathlib.Path | None = None,
) -> dict[str, tuple[float, float]]:
    """{metric family: (lowest finite bound, top finite bound)} from
    histogram_bounds.txt.

    Comment and blank lines (including the `# --- <section> ---` headers, which
    only reconcile_metric_provenance.py routes on) are skipped.
    """
    out: dict[str, tuple[float, float]] = {}
    for line in (path or _bounds_path()).read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _BOUNDS_ENTRY.match(stripped)
        if not m:
            continue
        try:
            low, high = float(m.group("low")), float(m.group("high"))
        except ValueError as exc:
            raise ValueError(f"histogram_bounds.txt: bad entry {stripped!r}") from exc
        if not (math.isfinite(low) and math.isfinite(high)) or low >= high:
            raise ValueError(
                f"histogram_bounds.txt: {stripped!r} - expected finite "
                "`<family> <lowest finite bound> <top finite bound>` with lowest < top"
            )
        out[m.group("family")] = (low, high)
    return out


def _rounded(value: float, decimal_points: int) -> float:
    """value through main.tf's `round($C * 10^d) / 10^d` reduce step, with Go's
    math.Round semantics (half away from zero) rather than Python's banker's.

    Applying the rounding to an endpoint is sound because round is monotone
    non-decreasing and both endpoints of the reachable interval are attained, so
    round(max) is the max of the rounded image. `10.0 **` rather than `10 **`
    keeps a large decimal_points from building an unconvertible Python int.
    """
    try:
        scale = 10.0**decimal_points
        scaled = value * scale
    except OverflowError:
        return value
    if not math.isfinite(scale) or scale == 0 or not math.isfinite(scaled):
        return value
    return (
        math.floor(scaled + 0.5) / scale
        if scaled >= 0
        else -math.floor(-scaled + 0.5) / scale
    )


def _quantile_floor(low: float, q: float) -> float:
    """The smallest value histogram_quantile(q, ...) can return over a histogram
    whose lowest finite bucket bound is `low`. See the block comment above: the
    b == 0 branch bottoms out at q*low, except that bucketQuantile short-circuits
    a non-positive lowest bound and returns it directly."""
    return low if low <= 0 else q * low


def _threshold_reachable(
    floor: float, ceiling: float, threshold: float, operator: str
) -> bool:
    """Can the post-reduce quantile, whose range is [floor, ceiling], satisfy
    `value <operator> threshold` for any input?"""
    if operator == ">":
        return ceiling > threshold
    if operator == ">=":
        return ceiling >= threshold
    if operator == "<":
        return floor < threshold
    if operator == "<=":
        return floor <= threshold
    return True  # an operator this check does not model is not judged


_AND_KEYWORD = re.compile(r"(?<![A-Za-z0-9_:])(?:and|unless)(?![A-Za-z0-9_:])")


def _truncate_at_top_level_and(expr: str) -> str:
    """expr truncated at its first TOP-LEVEL `and`/`unless`.

    PromQL's `and` and `unless` are set FILTERS: `A and B` yields A's samples,
    keeping only those whose label set also appears in B. The value a threshold is
    compared against is therefore always the LEFT operand, whatever shape the right
    one has - `and` binds looser than every arithmetic and comparison operator, so
    `A and (g) > B and (h)` is `A and ((g) > B) and (h)` and its value is still A.

    This used to match the guard by SHAPE (a trailing `and [on(...)] (`), which is
    defeated by any guard the author writes differently: `and on() sum(...) > 0`
    without wrapping parens dropped the rule out of the check entirely, and
    `(guard) and on() histogram_quantile(...)` was read as a quantile rule when its
    value is the guard's. Truncating cannot be defeated that way.

    Top-level means paren/bracket/brace depth 0 and outside a quoted label value,
    so `{kind=~"brainstorm and review"}` is not an operator.
    """
    depth = 0
    quote = ""
    for i, ch in enumerate(expr):
        if quote:
            if ch == quote and expr[i - 1 : i] != "\\":
                quote = ""
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch in "au":
            m = _AND_KEYWORD.match(expr, i)
            if m:
                return expr[:i]
    return expr


# The constant must accept every Go/PromQL float spelling, scientific notation
# included: a `vector(1e3)` the regex cannot read is not merely an unknown
# constant, it also stops the expression reading as a bare quantile and drops the
# whole rule out of the check.
_OR_VECTOR = re.compile(
    r"\s+or\b(?:\s+on\s*\([^)]*\))?\s+vector\s*\(\s*"
    r"(-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*\)"
)


def _strip_wrapping_parens(expr: str) -> str:
    """expr with fully-enclosing paren pairs removed. `(x)` -> `x`, but `(a)+(b)`
    is left alone: the leading `(` there does not match the final `)`."""
    expr = expr.strip()
    while expr.startswith("("):
        if len(_call_arguments(expr, 0)) != len(expr) - 2:
            return expr
        expr = expr[1:-1].strip()
    return expr


def _normalised_value_expression(expr: str) -> tuple[str, list[float]]:
    """(the expression whose value the threshold compares against, the constants
    any `or vector(N)` can substitute for it).

    Idle guards contribute no value, wrapping parens contribute no value, and
    `or vector(N)` adds N to the reachable set without touching the ceiling.

    `or vector(N)` is extracted BEFORE the `and` truncation, not after: `or` binds
    looser than `and`, so `A and (g) or vector(0)` is `(A and (g)) or vector(0)` and
    truncating first would swallow the constant along with the guard.
    """
    stripped = _strip_wrapping_parens(expr)
    constants = [float(m.group(1)) for m in _OR_VECTOR.finditer(stripped)]
    if constants:
        stripped = _strip_wrapping_parens(_OR_VECTOR.sub("", stripped))
    return _strip_wrapping_parens(_truncate_at_top_level_and(stripped)), constants


def _is_bare_quantile(expr: str) -> bool:
    """True when the normalised expression is nothing but one histogram_quantile(
    call, so its value is in the histogram's own units.

    A scaled or aggregated quantile (`1000 * histogram_quantile(...)` for
    milliseconds, `sum(histogram_quantile(...))` across pods, a quantile compared
    against another quantile) compares against a derived quantity this check does
    not model; range-checking those against the raw bucket ceiling would fail a
    correct rule, which is how a check gets weakened to a warning. They are
    skipped - but the skip is DECLARED, not silent: see lint_histogram_range.
    """
    m = _HISTOGRAM_QUANTILE.match(expr)
    if m is None:
        return False
    args = _call_arguments(expr, m.end() - 1)
    return expr[m.end() + len(args) :].strip() == ")"


def _quantile_call_quantiles(expr: str) -> list[float | None]:
    """One entry per histogram_quantile( call in expr, in order: the literal `q`
    of that call, or None when it is not a plain numeric literal."""
    out: list[float | None] = []
    for m in _HISTOGRAM_QUANTILE.finditer(expr):
        args = _call_arguments(expr, m.end() - 1)
        head = args.split(",", 1)[0].strip()
        try:
            out.append(float(head))
        except ValueError:
            out.append(None)
    return out


def lint_histogram_range(
    path: str, rule: dict, bounds: dict[str, tuple[float, float]]
) -> Violation | None:
    joined = _joined_expressions(rule)
    if not _quantile_call_families(joined):
        return None
    annotations = rule.get("annotations") or {}
    if str(annotations.get(HISTOGRAM_RANGE_ANNOTATION_KEY, "")).strip():
        return None
    name = rule.get("name", "<unnamed>")
    # Everything below reads the NORMALISED expression, not the raw one: a
    # quantile on the right of an `and` contributes no value to the threshold
    # comparison, and range-checking its ceiling against this rule's threshold
    # would fail a correct rule.
    value_expr, or_vector_constants = _normalised_value_expression(joined)
    if not _HISTOGRAM_QUANTILE.search(value_expr):
        # Every quantile in this rule sits on the right of an `and`/`unless`, so it
        # filters the value rather than being it. There is no quantile threshold to
        # range-check, and demanding a declaration would be a red build on a rule
        # that is not in this check's subject at all.
        return None
    if not _is_bare_quantile(value_expr):
        # The shape is out of model (see _is_bare_quantile) and is NOT range-checked
        # - but it is not waved through in silence either. A skip nobody can grep is
        # the same bypass the unknown-family failure below exists to close, so the
        # carve-out has to be declared on the rule, exactly as the other three checks
        # require their own annotation.
        return Violation(
            path,
            name,
            "compares a threshold against something other than a bare "
            "histogram_quantile() (a scaled or aggregated quantile, a quantile-vs-"
            "quantile comparison, or a multi-query rule), so its threshold is in "
            "derived units this check cannot range-check against the histogram's "
            "buckets. A quantile threshold outside the reachable range makes a rule "
            "structurally inert and it reports OK forever. Set a non-empty "
            f"`{HISTOGRAM_RANGE_ANNOTATION_KEY}` annotation recording why the "
            "threshold is inside the reachable range. See CONVENTIONS.md.",
        )
    families = _quantile_call_families(value_expr)
    quantiles = _quantile_call_quantiles(value_expr)
    operator = str(rule.get("math_operator", ">")).strip()
    try:
        threshold = float(rule.get("threshold"))
        decimal_points = int(rule.get("decimal_points", 2))
    except (TypeError, ValueError):
        return None  # check_alert_schema.py owns malformed keys
    for family, q in zip(families, quantiles):
        if family is None:
            return Violation(
                path,
                name,
                "uses histogram_quantile() with no identifiable `<metric>_bucket` "
                "selector in its own arguments, so the range its threshold is "
                "compared against cannot be verified: a threshold outside the "
                "reachable range makes the rule structurally inert and it reports "
                "OK forever. Use a recognisable `<metric>_bucket` selector, or set "
                f"a non-empty `{HISTOGRAM_RANGE_ANNOTATION_KEY}` annotation (Check 2 "
                f"reports this same shape and needs `{QUANTILE_ANNOTATION_KEY}` "
                "separately). See CONVENTIONS.md.",
            )
        if family not in bounds:
            return Violation(
                path,
                name,
                f"runs histogram_quantile() over `{family}_bucket`, which has no "
                "bucket-range entry in `scripts/histogram_bounds.txt`, so its "
                "threshold cannot be range-checked. Add a "
                "`<family> <lowest finite bound> <top finite bound>` line there "
                "(reconcile_metric_provenance.py validates it "
                "against the producer's Go source), or set a non-empty "
                f"`{HISTOGRAM_RANGE_ANNOTATION_KEY}` annotation. See CONVENTIONS.md.",
            )
        low, high = bounds[family]
        if q is None and operator in ("<", "<="):
            continue  # the floor is q-dependent and q is not a literal here
        ceiling = _rounded(high, decimal_points)
        floor = _rounded(_quantile_floor(low, q if q is not None else 0.0), decimal_points)
        for constant in or_vector_constants:
            # `or vector(N)` substitutes N when the vector is empty, so N joins the
            # reachable set. It can widen the range in either direction.
            rounded = _rounded(constant, decimal_points)
            floor, ceiling = min(floor, rounded), max(ceiling, rounded)
        if _threshold_reachable(floor, ceiling, threshold, operator):
            continue
        return Violation(
            path,
            name,
            f"compares a histogram_quantile() over `{family}_bucket` against "
            f"`{operator} {threshold:g}`, which that quantile can never satisfy. "
            f"Its buckets run {low:g}..{high:g}, so the quantile's reachable range "
            f"is [{floor:g}, {ceiling:g}] at decimal_points: {decimal_points} - "
            "classic histogram_quantile returns at most the top finite bucket bound, "
            f"and at least q times the lowest one. The rule is structurally inert - "
            "it cannot fire on any input, and default_no_data_state: \"OK\" means it "
            "never goes stale either. Move the threshold inside the range, widen the "
            f"producer's buckets, or set a non-empty "
            f"`{HISTOGRAM_RANGE_ANNOTATION_KEY}` annotation. See CONVENTIONS.md.",
        )
    return None


# --- Check 5: dashboard threshold step outside the panel's reachable range ---
#
# Check 4 walks alerts/*.yaml only, and the SAME defect was live one surface over
# the whole time it was being written: dashboards/operator.json and
# dashboards/memory.json both painted a red threshold step at 30 over the same two
# histograms, topping out at 25.6 and 10 (#111 review round 1). A Grafana threshold
# step colours a value at `value >= step`, so a step above the top finite bucket
# bound never colours: the panel reads healthy because it cannot read anything else,
# and unlike an alert there is not even a no_data_state to mis-configure.
# check_metric_provenance.py already treats dashboards/*.json as a first-class
# silent-green surface (CONVENTIONS.md section 5); this is the comparison dimension
# of the same surface.
#
# SCOPE, deliberately narrow to keep the check false-failure free: a panel is
# range-checked only when it carries at least one finite ABSOLUTE threshold step AND
# every one of its Prometheus targets is a bare histogram_quantile() (so the step is
# in the histogram's own units). A panel mixing a quantile with a rate, or scaling a
# quantile into milliseconds, is skipped: a threshold step applies to every series in
# the panel, so a single out-of-model series leaves the panel's reachable maximum
# unbounded and NOTHING can be concluded. That is a known false negative - adding a
# non-quantile overlay to either of the two panels this check was written for turns it
# off - and a panel has no annotations, so there is no per-panel escape hatch to
# declare. CONVENTIONS.md 6.5 is the record instead.
#
# Only the unreachable direction is failed; a step at or below the floor (a
# permanently-red band) is a different defect and is not modelled here. The verdict is
# computed from ALL targets before anything is reported, so it cannot depend on the
# order the targets happen to sit in the JSON.
#
# decimal_points has no analogue: Grafana's fieldConfig `decimals` is display
# formatting and does not round the value a threshold step is evaluated against.
def _panel_datasource_type(target: dict, panel: dict) -> str:
    """The datasource type for one target, following the panel-level default.
    Grafana's legacy form is a bare name string rather than a {type, uid} object."""
    ds = target.get("datasource") or panel.get("datasource") or {}
    if isinstance(ds, str):
        return "prometheus" if ds.strip().lower() == "prometheus" else ds.strip()
    return str(ds.get("type", "")) if isinstance(ds, dict) else ""


def _step_value(step: object) -> float | None:
    """One threshold step's value as a float, or None for the base step.

    The base step carries `value: null` - it is the floor colour, not a comparison.
    Grafana coerces a numeric string, so `"30"` is a step like `30` is.
    """
    if not isinstance(step, dict):
        return None
    value = step.get("value")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _absolute_steps(thresholds: object) -> list[float]:
    """Finite step values of one thresholds object, ONLY in absolute mode.

    In `percentage` mode a step is a percentage of the field's min..max, not a value
    in the metric's units, so range-checking it against the bucket ceiling would fail
    a correct panel. Absolute is Grafana's default when mode is absent.
    """
    if not isinstance(thresholds, dict):
        return []
    if str(thresholds.get("mode", "absolute")).lower() != "absolute":
        return []
    return [
        v
        for v in (_step_value(step) for step in thresholds.get("steps") or [])
        if v is not None
    ]


def _panel_threshold_steps(panel: dict) -> list[float]:
    """Every finite absolute threshold step on a panel - the field defaults plus any
    per-series `overrides[].properties[].id == "thresholds"`, an idiom this repo uses
    in dashboards/task-delivery.json. A red step hidden in an override colours just
    as unreachably as one in the defaults."""
    field_config = panel.get("fieldConfig") or {}
    defaults = field_config.get("defaults") or {}
    if str(
        ((defaults.get("custom") or {}).get("thresholdsStyle") or {}).get("mode", "")
    ).lower() == "off":
        # The band is never drawn, so a leftover step has no rendered effect. Most
        # timeseries panels here set this; failing on them would be a red build on a
        # panel with nothing wrong with it.
        return []
    out = _absolute_steps(defaults.get("thresholds"))
    for override in field_config.get("overrides") or []:
        if not isinstance(override, dict):
            continue
        for prop in override.get("properties") or []:
            if isinstance(prop, dict) and prop.get("id") == "thresholds":
                out.extend(_absolute_steps(prop.get("value")))
    return out


def lint_dashboard_panel(
    path: str, panel: dict, bounds: dict[str, tuple[float, float]]
) -> Violation | None:
    exprs = [
        str(t.get("expr") or "")
        for t in (panel.get("targets") or [])
        if _panel_datasource_type(t, panel) == "prometheus" and (t.get("expr") or "")
    ]
    if not exprs or not any(_HISTOGRAM_QUANTILE.search(e) for e in exprs):
        return None
    steps = _panel_threshold_steps(panel)
    if not steps:
        return None  # nothing compares against the quantile
    title = f'panel "{panel.get("title", "<untitled>")}"'

    # Classify EVERY target first. Reporting from inside the scan would make both
    # the verdict and the message depend on target order.
    ceiling: float | None = None
    unknown: list[str] = []
    for expr in exprs:
        value_expr, or_vector_constants = _normalised_value_expression(expr)
        if not _is_bare_quantile(value_expr):
            return None  # derived units, or a non-quantile series: unbounded panel
        families = _quantile_call_families(value_expr)
        if not families or any(f is None for f in families):
            return None  # no identifiable _bucket selector; nothing to range-check
        for family in families:
            if family not in bounds:
                unknown.append(family)
                continue
            high = bounds[family][1]
            ceiling = high if ceiling is None else max(ceiling, high)
        for constant in or_vector_constants:
            ceiling = constant if ceiling is None else max(ceiling, constant)

    if unknown:
        return Violation(
            path,
            title,
            "plots histogram_quantile() over "
            + ", ".join(f"`{f}_bucket`" for f in sorted(set(unknown)))
            + " under a threshold step, but that family has no bucket-range entry "
            "in `scripts/histogram_bounds.txt`, so the step cannot be range-checked. "
            "Add a `<family> <lowest finite bound> <top finite bound>` line there "
            "(reconcile_metric_provenance.py validates it against the producer's Go "
            "source). See CONVENTIONS.md.",
        )
    if ceiling is None:
        return None
    unreachable = sorted(s for s in steps if s > ceiling)
    if not unreachable:
        return None
    return Violation(
        path,
        title,
        "paints a threshold step at "
        + ", ".join(f"{s:g}" for s in unreachable)
        + f", above the {ceiling:g} its histogram_quantile() can ever return "
        "(classic histogram_quantile returns at most the top finite bucket bound). "
        "The band never colours, so the panel reads healthy because it cannot read "
        "anything else - the same silent-green class as an inert alert rule, with "
        "no no_data_state to even mis-configure. Move the step inside the range or "
        "widen the producer's buckets. See CONVENTIONS.md.",
    )


def lint_dashboard_file(path: str, bounds: dict[str, tuple[float, float]]) -> list[Violation]:
    data = json.loads(pathlib.Path(path).read_text())
    out: list[Violation] = []

    def walk(panels: list[dict]) -> None:
        for panel in panels:
            if not isinstance(panel, dict):
                continue
            v = lint_dashboard_panel(path, panel, bounds)
            if v is not None:
                out.append(v)
            walk(panel.get("panels") or [])  # row-collapsed panels

    walk(data.get("panels") or [])
    return out


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


def lint_file(
    path: str, bounds: dict[str, tuple[float, float]] | None = None
) -> list[Violation]:
    if bounds is None:
        bounds = load_histogram_bounds()
    if path.endswith(".json"):
        return lint_dashboard_file(path, bounds)
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
    return sorted(glob.glob(str(root / "alerts" / "*.yaml"))) + sorted(
        glob.glob(str(root / "dashboards" / "*.json"))
    )


def main(argv: list[str]) -> int:
    paths = argv[1:] or _default_paths()
    if not paths:
        print("lint_alert_rules: no alert files found", file=sys.stderr)
        return 2
    try:
        violations = lint_paths(paths)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"lint_alert_rules: {exc}", file=sys.stderr)
        return 2
    if violations:
        print(f"FAIL: {len(violations)} rule(s)/panel(s) violate the tatara alert conventions:\n")
        for v in violations:
            print(f"  - {v}")
        print("\nEach message names its own fix. See CONVENTIONS.md.")
        return 1
    print(f"OK: {len(paths)} alert/dashboard file(s) pass the tatara alert conventions lint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
