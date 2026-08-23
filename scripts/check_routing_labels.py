#!/usr/bin/env python3
"""Fail CI when an alert rule's labels do not satisfy the notification-routing contract.

THE DELIVERY TRAP. Every other guard in this repo asserts that a rule is well-formed,
that its metric exists, that its labels exist on that metric, that its threshold is
inside its histogram's range, that its runbook anchor resolves. All of them assume the
rule, once firing, is DELIVERED. Delivery is decided by two hand-copied label strings
that nothing verified until this file existed.

The live policy tree (infra/terraform/grafana, read from Grafana):

    root                        receiver=Default
    `-- homelab="true"          receiver=Default, mute_time_intervals=["Default"]
        |-- system="tatara"     receiver=Tatara      <- the operator incident webhook
        |-- severity="critical" receiver=Critical
        `-- page="true"         receiver=Critical

First matching child wins; no route sets `continue`; the `Default` mute interval is all
day Saturday and Sunday; the `Default` contact point is a single email address. So:

| defect                              | lands on             | consequence                          |
|-------------------------------------|----------------------|--------------------------------------|
| warning/critical rule omits `system` | the `homelab` node   | NEVER mints an incident Task. Emails, |
|                                     |                      | and is muted every weekend            |
| info rule gains `system`            | the `system` child   | mints an incident Task per fire, for  |
|                                     |                      | a trend nobody should be paged on     |
| any rule omits `homelab`            | root                 | loses the mute window and grouping    |

Row 1 is the one with teeth. A rule can be authored, pass all seven other guards,
`terraform validate`, plan, apply green, evaluate correctly and fire correctly - and
never reach the agent platform. That is the same silent-green shape this repo already
closed for metric names (check_metric_provenance.py), label names
(check_label_provenance.py) and schema keys (check_alert_schema.py).

BIDIRECTIONAL, AND KEYED ON SEVERITY. `info` rules DELIBERATELY omit `system`: the
system=tatara route is what turns a firing alert into an incident Task, so an
info-severity trend rule carrying it mints an incident for a condition nobody should be
paged on. This is not hypothetical either - the two trend rules added for #457
(repository phase desync, ingest job dedup race) shipped with system=tatara and were
corrected by a human reading label sets, not by CI. A checker that "harmonises" the
label sets across severities to turn the corpus green has re-created that bug.

    | severity | homelab="true" | system="tatara" |
    |----------|----------------|-----------------|
    | critical | required       | required        |
    | warning  | required       | required        |
    | info     | required       | must be ABSENT  |

AN UNRECOGNISED SEVERITY IS A HARD FAILURE, NOT A PASS. Falling through to an `else`
arm is how a future fourth severity silently acquires whichever routing behaviour the
branch happened to have.

WHY `labels` MUST BE NON-EMPTY. `modules/grafana_alert/main.tf:124` is a ternary, not a
merge: declared labels REPLACE `default_labels`. Nothing wires `default_labels` any more
(grafana.tf), so the false branch now renders NO labels at all - such a rule routes to
the root receiver. The input stays declared in the vendored module for byte-alignment
with infra/terraform; this check is what proves the branch stays unreachable.

THE WAIVER. A rule-level `tatara_routing_justification` (non-empty string) waives the
severity=>system arm for that rule only, and the waived rules are printed so the
override is visible in CI output as well as in the diff. It does NOT waive `homelab`,
severity validity, or non-empty `labels`: there is no legitimate reason for a tatara
alert to leave the homelab subtree. The key is registered in check_alert_schema.py's
LINT_ONLY_KEYS and relies on the silent drop to stay out of Grafana; see CONVENTIONS.md
section 10.

`component` is deliberately NOT asserted: it is not part of the routing predicate.

Run: python3 scripts/check_routing_labels.py [alerts/*.yaml]
Exit 0 = clean, 1 = violations, 2 = usage/parse error.
"""

from __future__ import annotations

import glob
import pathlib
import sys

import yaml

HOMELAB_KEY = "homelab"
HOMELAB_VALUE = "true"
SYSTEM_KEY = "system"
SYSTEM_VALUE = "tatara"
SEVERITY_KEY = "severity"
JUSTIFICATION_KEY = "tatara_routing_justification"

# The severities the policy tree has an answer for. Extending this set is a routing
# decision, not a lint decision: a new value needs a route (or a deliberate arm here)
# before a rule may carry it.
ROUTED_SEVERITIES = ("critical", "warning")
UNROUTED_SEVERITIES = ("info",)
KNOWN_SEVERITIES = ROUTED_SEVERITIES + UNROUTED_SEVERITIES


class Violation:
    """One rule whose routing labels would misdeliver it."""

    def __init__(self, path: str, rule: str, message: str):
        self.path = path
        self.rule = rule
        self.message = message

    def __str__(self) -> str:
        return f'{self.path}: rule "{self.rule}" {self.message}'


class Waiver:
    """One rule that opted out of the severity=>system arm, and why."""

    def __init__(self, path: str, rule: str, justification: str):
        self.path = path
        self.rule = rule
        self.justification = justification

    def __str__(self) -> str:
        return f'{self.path}: rule "{self.rule}" - {self.justification}'


def justification_of(rule: dict) -> str:
    """The rule's waiver text, or "" if it has none. A non-string (a bare YAML `true`,
    a number) is not a justification - the point of the key is that a human reading the
    diff learns why."""
    raw = rule.get(JUSTIFICATION_KEY)
    return raw.strip() if isinstance(raw, str) else ""


def check_rule(path: str, rule: dict) -> list[Violation]:
    """Every routing defect on one rule. Empty list = the rule is deliverable."""
    name = rule.get("name", "<unnamed>")
    labels = rule.get("labels") or {}
    if not labels:
        return [
            Violation(
                path,
                name,
                "declares no `labels`. main.tf's ternary then falls back to the module's "
                "`default_labels`, which nothing wires any more, so the rule would render "
                "with NO labels and route to the root receiver. Every rule must declare "
                f"at least {HOMELAB_KEY}/{SEVERITY_KEY} (see CONVENTIONS.md section 10).",
            )
        ]

    out: list[Violation] = []
    homelab = labels.get(HOMELAB_KEY)
    if homelab != HOMELAB_VALUE:
        out.append(
            Violation(
                path,
                name,
                f"has `{HOMELAB_KEY}: {homelab!r}`, expected {HOMELAB_VALUE!r}. Without it "
                "the rule never enters the homelab subtree: it falls to the root receiver, "
                "losing the weekend mute window and the grouping. This is not waivable.",
            )
        )

    severity = labels.get(SEVERITY_KEY)
    if severity not in KNOWN_SEVERITIES:
        out.append(
            Violation(
                path,
                name,
                f"has `{SEVERITY_KEY}: {severity!r}`, which is not one of "
                f"{', '.join(KNOWN_SEVERITIES)}. The routing contract is keyed on severity, "
                "so an unrecognised value has no defined delivery. Add a route (and an arm "
                "here) before using a new severity.",
            )
        )
        return out

    waived = bool(justification_of(rule))
    has_system = SYSTEM_KEY in labels
    if severity in ROUTED_SEVERITIES and labels.get(SYSTEM_KEY) != SYSTEM_VALUE:
        if not waived:
            out.append(
                Violation(
                    path,
                    name,
                    f"is `{SEVERITY_KEY}: {severity}` but has "
                    f"`{SYSTEM_KEY}: {labels.get(SYSTEM_KEY)!r}`, expected {SYSTEM_VALUE!r}. "
                    "It would fall past the system=tatara route to the homelab node: email "
                    "only, muted all weekend, and it would NEVER mint an incident Task. If "
                    f"that is deliberate, add a `{JUSTIFICATION_KEY}` to the rule.",
                )
            )
    elif severity in UNROUTED_SEVERITIES and has_system:
        if not waived:
            out.append(
                Violation(
                    path,
                    name,
                    f"is `{SEVERITY_KEY}: {severity}` but carries "
                    f"`{SYSTEM_KEY}: {labels.get(SYSTEM_KEY)!r}`, which must be absent. The "
                    "system=tatara route mints an incident Task per fire, and an info-level "
                    "trend rule is not something anyone should be paged on (this is the "
                    "#457 regression). If this condition really must mint a Task, add a "
                    f"`{JUSTIFICATION_KEY}` to the rule.",
                )
            )
    return out


def _waiver_applies(rule: dict) -> bool:
    """True when the rule's justification actually suppressed a severity=>system
    violation - not merely that the key is present."""
    if not justification_of(rule):
        return False
    labels = rule.get("labels") or {}
    severity = labels.get(SEVERITY_KEY)
    if severity in ROUTED_SEVERITIES:
        return labels.get(SYSTEM_KEY) != SYSTEM_VALUE
    if severity in UNROUTED_SEVERITIES:
        return SYSTEM_KEY in labels
    return False


def check_paths(paths: list[str]) -> tuple[list[Violation], list[Waiver]]:
    """Run the contract over every rule in every file."""
    violations: list[Violation] = []
    waivers: list[Waiver] = []
    for path in paths:
        data = yaml.safe_load(pathlib.Path(path).read_text())
        if not data or not isinstance(data, dict):
            continue
        for rule in data.get("rules") or []:
            violations.extend(check_rule(path, rule))
            if _waiver_applies(rule):
                waivers.append(
                    Waiver(path, rule.get("name", "<unnamed>"), justification_of(rule))
                )
    return violations, waivers


def _default_paths() -> list[str]:
    root = pathlib.Path(__file__).resolve().parent.parent
    return sorted(glob.glob(str(root / "alerts" / "*.yaml")))


def _count_rules(paths: list[str]) -> int:
    total = 0
    for path in paths:
        data = yaml.safe_load(pathlib.Path(path).read_text())
        if isinstance(data, dict):
            total += len(data.get("rules") or [])
    return total


def main(argv: list[str]) -> int:
    paths = argv[1:] or _default_paths()
    if not paths:
        print("check_routing_labels: no alert files found", file=sys.stderr)
        return 2
    try:
        violations, waivers = check_paths(paths)
        total = _count_rules(paths)
    except (OSError, yaml.YAMLError) as exc:
        print(f"check_routing_labels: {exc}", file=sys.stderr)
        return 2

    if waivers:
        print(
            f"{len(waivers)} rule(s) waive the severity=>system arm via "
            f"`{JUSTIFICATION_KEY}`:\n"
        )
        for w in waivers:
            print(f"  - {w}")
        print()

    if violations:
        print(
            f"FAIL: {len(violations)} routing-label violation(s) across {total} alert "
            "rule(s):\n"
        )
        for v in violations:
            print(f"  - {v}")
        print(
            "\nRouting is decided entirely by these labels. A rule that fails here can "
            "still lint, plan, apply green and fire - and be delivered to the wrong place "
            "or to nowhere. See CONVENTIONS.md section 10."
        )
        return 1

    print(
        f"OK: {total} alert rule(s) satisfy the routing-label contract "
        f"({len(waivers)} waived)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
