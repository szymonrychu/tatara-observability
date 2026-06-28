#!/usr/bin/env python3
"""Lint tatara alert rules for the benign/transient classification convention.

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
    def __init__(self, path: str, rule: str, metric: str):
        self.path = path
        self.rule = rule
        self.metric = metric

    def __str__(self) -> str:
        return (
            f"{self.path}: rule \"{self.rule}\" selects server errors on "
            f"`{self.metric}` but neither excludes probe routes in the selector "
            f"(e.g. route!~\"/readyz|/healthz|/metrics\") nor sets a non-empty "
            f"`{ANNOTATION_KEY}` annotation. See CONVENTIONS.md."
        )


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
    queries = rule.get("queries") or []
    joined = "\n".join(q.get("expression", "") or "" for q in queries)
    metric = _error_http_metric(joined)
    if metric is None:
        return None  # not an http error-ratio rule; out of scope for this lint
    if _PROBE_FILTER.search(joined):
        return None  # consumer-side filter present
    annotations = rule.get("annotations") or {}
    if str(annotations.get(ANNOTATION_KEY, "")).strip():
        return None  # justified
    return Violation(path, rule.get("name", "<unnamed>"), metric)


def lint_file(path: str) -> list[Violation]:
    data = yaml.safe_load(pathlib.Path(path).read_text())
    if not data or not isinstance(data, dict):
        return []
    out = []
    for rule in data.get("rules") or []:
        v = lint_rule(path, rule)
        if v is not None:
            out.append(v)
    return out


def lint_paths(paths: list[str]) -> list[Violation]:
    out = []
    for p in paths:
        out.extend(lint_file(p))
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
        print(f"FAIL: {len(violations)} alert rule(s) violate the benign/transient convention:\n")
        for v in violations:
            print(f"  - {v}")
        print(
            "\nFix: add a probe-route exclusion to the selector, OR set a "
            f"`{ANNOTATION_KEY}` annotation citing where probes are excluded "
            "(producer-side) or the tracked follow-up. See CONVENTIONS.md."
        )
        return 1
    print(f"OK: {len(paths)} alert file(s) pass the benign/transient convention lint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
