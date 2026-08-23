#!/usr/bin/env python3
"""Fail CI when an alert rule's labels do not satisfy the notification-routing contract.

THE DELIVERY TRAP. Every other guard in this repo asserts that a rule is well-formed,
that its metric exists, that its labels exist on that metric, that its threshold is
inside its histogram's range, that its runbook anchor resolves. All of them assume the
rule, once firing, is DELIVERED. Delivery is decided by label strings restated by hand
in every rule, and nothing verified them until this file existed.

The live policy tree (owned by infra/terraform/grafana, read from Grafana):

    root                        receiver=Default
    `-- homelab="true"          receiver=Default,  mute_time_intervals=["Default"]
        |-- system="tatara"     receiver=Tatara    -> /operator/webhooks/tatara/grafana
        |-- severity="critical" receiver=Critical
        `-- page="true"         receiver=Critical

First matching child wins; no route sets `continue`. `Default` mutes all day Saturday
and Sunday and is one email address. `Critical` is an unmuted email plus a webhook to
/operator/webhooks/INFRASTRUCTURE/grafana, repeat_interval 4h - a different project's
endpoint, so it does not mint a tatara incident Task either.

| defect                        | lands on              | consequence                     |
|-------------------------------|-----------------------|---------------------------------|
| `warning` omits `system`      | the `homelab` node    | one email, muted all weekend,   |
|                               |                       | NEVER mints an incident Task    |
| `critical` omits `system`     | `severity=critical`   | Critical receiver, not muted -  |
|                               |                       | still never mints a tatara Task |
| `info` gains `system=tatara`  | `system=tatara`       | an incident Task per fire       |
| any rule carries `page`       | `page=true`           | escalates an unrouted rule to   |
|                               |                       | Critical; inert on a routed one |
| any rule omits `homelab`      | root                  | loses the mute window, grouping |

Rows 1 and 2 are the ones with teeth. Such a rule passes all seven other guards, passes
`terraform validate`, plans, applies green, evaluates correctly, fires correctly - and
never reaches the agent platform. That is the same silent-green shape this repo already
closed for metric names (check_metric_provenance.py), label names
(check_label_provenance.py) and schema keys (check_alert_schema.py).

BIDIRECTIONAL, AND KEYED ON SEVERITY. `info` rules DELIBERATELY omit `system`: the
system=tatara route is what turns a firing alert into an incident Task, so an
info-severity trend rule carrying it mints an incident for a condition nobody should be
paged on. This is not hypothetical either - the two trend rules added for #457
(repository phase desync, ingest job dedup race) shipped with system=tatara and were
corrected by a human reading label sets, not by CI. A checker that "harmonises" the
label sets across severities to turn the corpus green has re-created that bug.

    | severity | homelab="true" | system="tatara" | page   |
    |----------|----------------|-----------------|--------|
    | critical | required       | required        | absent |
    | warning  | required       | required        | absent |
    | info     | required       | must be ABSENT  | absent |

AN UNRECOGNISED SEVERITY IS A HARD FAILURE, NOT A PASS. Falling through to an `else`
arm is how a future fourth severity silently acquires whichever routing behaviour the
branch happened to have.

WHY `labels` MUST BE NON-EMPTY. `modules/grafana_alert/main.tf:124` is a ternary, not a
merge: declared labels REPLACE `default_labels`. Nothing wires `default_labels` any more
(grafana.tf), so the false branch now renders NO labels at all - such a rule routes to
the root receiver. The input stays declared in the vendored module for byte-alignment
with infra/terraform; this check is what proves the branch stays unreachable.

TERRAFORM COERCION IS PART OF THE PREDICATE. `labels` is typed `map(string)`, so an
unquoted YAML `homelab: true` reaches Grafana as the string "true" and routes correctly.
Values are compared after the same coercion, because a red build on a rule with no
routing defect is how a guard gets argued down to a warning.

THE WAIVER. A rule-level `tatara_routing_justification` (non-empty string) waives the
severity-keyed `system` and `page` arms for that rule only, and the waived rules are
printed so the override is visible in CI output as well as in the diff. It does NOT
waive `homelab`, severity validity, or non-empty `labels`: there is no legitimate reason
for a tatara alert to leave the homelab subtree. A justification that suppresses nothing
is itself a violation - left alone it is invisible to review and silently ARMS itself
the day someone drops `system` from that rule, with a reason written for a condition
that no longer exists. The key is registered in check_alert_schema.py's LINT_ONLY_KEYS
and relies on the silent drop to stay out of Grafana; see CONVENTIONS.md section 10.

`component` is deliberately NOT asserted: it is not part of the routing predicate.

Run: python3 scripts/check_routing_labels.py [alerts/*.yaml]
Exit 0 = clean, 1 = violations, 2 = usage/parse error (including an alert file whose
shape this script cannot walk - a guard that cannot read its input must fail loudly).
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
PAGE_KEY = "page"
SEVERITY_KEY = "severity"
JUSTIFICATION_KEY = "tatara_routing_justification"

# The severities the policy tree has an answer for. Extending this set is a routing
# decision, not a lint decision: a new value needs a route (or a deliberate arm here)
# before a rule may carry it.
ROUTED_SEVERITIES = ("critical", "warning")
UNROUTED_SEVERITIES = ("info",)
KNOWN_SEVERITIES = ROUTED_SEVERITIES + UNROUTED_SEVERITIES

# Where a routed rule that dropped `system` actually lands. The two severities differ,
# and saying "muted all weekend" to the author of a critical rule sends them looking for
# a mute window that does not apply to them.
_NO_SYSTEM_LANDS_ON = {
    "warning": (
        "It would fall past the system=tatara route to the homelab node: one email "
        "address, muted all day Saturday and Sunday, and it would NEVER mint an "
        "incident Task."
    ),
    "critical": (
        "It would fall past the system=tatara route and match severity=critical "
        "instead: the Critical receiver, which is an email plus the INFRASTRUCTURE "
        "webhook on a 4h repeat and no mute interval - so it still never mints a "
        "tatara incident Task."
    ),
}


class Violation:
    """One rule whose routing labels would misdeliver it."""

    def __init__(self, path: str, rule: str, message: str):
        self.path = path
        self.rule = rule
        self.message = message

    def __str__(self) -> str:
        return f'{self.path}: rule "{self.rule}" {self.message}'


class Waiver:
    """One rule that opted out of a severity-keyed arm, and why."""

    def __init__(self, path: str, rule: str, arms: list[str], justification: str):
        self.path = path
        self.rule = rule
        self.arms = arms
        self.justification = justification

    def __str__(self) -> str:
        return (
            f'{self.path}: rule "{self.rule}" waives {"+".join(self.arms)} - '
            f"{self.justification}"
        )


def as_label_string(value) -> str | None:
    """The value terraform's `map(string)` conversion hands to Grafana, or None when the
    label is absent/null. An unquoted YAML `true` is a bool here and the string "true"
    there; comparing the raw Python value would red-build a rule that routes correctly.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def justification_of(rule: dict) -> str:
    """The rule's waiver text, or "" if it has none. A non-string (a bare YAML `true`, a
    number) is not a justification - the point of the key is that a human reading the
    diff learns why."""
    raw = rule.get(JUSTIFICATION_KEY)
    return raw.strip() if isinstance(raw, str) else ""


def _evaluate(path: str, rule: dict) -> tuple[list[Violation], list[str]]:
    """(violations, names of the arms this rule's justification actually suppressed)."""
    name = rule.get("name", "<unnamed>")
    labels = rule.get("labels")
    if labels is not None and not isinstance(labels, dict):
        return [
            Violation(
                path,
                name,
                f"declares `labels` as {type(labels).__name__}, not a mapping. The module "
                "types it `map(string)`; terraform would reject it and the routing "
                "contract cannot be read off it.",
            )
        ], []
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
        ], []

    out: list[Violation] = []
    homelab = as_label_string(labels.get(HOMELAB_KEY))
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

    severity = as_label_string(labels.get(SEVERITY_KEY))
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
        return out, []

    justification = justification_of(rule)
    failed: list[tuple[str, str]] = []

    system = as_label_string(labels.get(SYSTEM_KEY))
    if severity in ROUTED_SEVERITIES and system != SYSTEM_VALUE:
        failed.append(
            (
                SYSTEM_KEY,
                f"is `{SEVERITY_KEY}: {severity}` but has `{SYSTEM_KEY}: {system!r}`, "
                f"expected {SYSTEM_VALUE!r}. {_NO_SYSTEM_LANDS_ON[severity]}",
            )
        )
    elif severity in UNROUTED_SEVERITIES and SYSTEM_KEY in labels:
        failed.append(
            (
                SYSTEM_KEY,
                f"is `{SEVERITY_KEY}: {severity}` but carries `{SYSTEM_KEY}: "
                f"{system!r}`, which must be absent. `{SYSTEM_KEY}` is the routing key "
                "that decides incident minting: with the value 'tatara' this rule would "
                "mint an incident Task on every fire, for a trend nobody should be paged "
                "on (the #457 regression), and with any other value it is a foreign "
                "platform's routing label on a tatara rule. severity: info means email "
                "only.",
            )
        )

    if PAGE_KEY in labels:
        failed.append(
            (
                PAGE_KEY,
                f"carries `{PAGE_KEY}: {as_label_string(labels.get(PAGE_KEY))!r}`. "
                f"`{PAGE_KEY}=true` is the third child route and reaches the same "
                "unmuted Critical receiver as severity=critical. It sits BELOW "
                "system=tatara, so on a routed rule it is inert and misleading, and on "
                "an info rule it escalates to a human the severity gate says must only "
                "be emailed. tatara rules route on severity, not on page.",
            )
        )

    waived = [key for key, _ in failed] if justification else []
    if justification and not waived:
        out.append(
            Violation(
                path,
                name,
                f"carries a `{JUSTIFICATION_KEY}` that waives nothing: its "
                f"`{SYSTEM_KEY}`/`{PAGE_KEY}` labels already satisfy the contract. A dead "
                "waiver is invisible to review and arms itself silently the day someone "
                "changes those labels, with a reason written for a condition that no "
                "longer exists. Delete it.",
            )
        )
    if not justification:
        out.extend(Violation(path, name, message) for _, message in failed)
    return out, waived


def check_rule(path: str, rule: dict) -> list[Violation]:
    """Every routing defect on one rule. Empty list = the rule is deliverable."""
    return _evaluate(path, rule)[0]


def _rules_of(path: str, data) -> list[dict]:
    """The rule list of one alert file, or raise ValueError naming the file. Every shape
    rejected here is one terraform's `merge`/`yamldecode` would also reject; reporting
    it as a traceback out of this script pointed the author at the wrong file."""
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top level is {type(data).__name__}, expected a mapping "
            "(one rule group per file)"
        )
    rules = data.get("rules")
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ValueError(
            f"{path}: `rules` is {type(rules).__name__}, expected a list of rules"
        )
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(
                f"{path}: rules[{index}] is {type(rule).__name__}, expected a mapping"
            )
    return rules


def check_paths(paths: list[str]) -> tuple[list[Violation], list[Waiver]]:
    """Run the contract over every rule in every file. Raises ValueError on a file whose
    shape cannot be walked."""
    violations: list[Violation] = []
    waivers: list[Waiver] = []
    for path in paths:
        data = yaml.safe_load(pathlib.Path(path).read_text())
        for rule in _rules_of(path, data):
            found, waived = _evaluate(path, rule)
            violations.extend(found)
            if waived:
                waivers.append(
                    Waiver(
                        path,
                        rule.get("name", "<unnamed>"),
                        waived,
                        justification_of(rule),
                    )
                )
    return violations, waivers


def _default_paths() -> list[str]:
    root = pathlib.Path(__file__).resolve().parent.parent
    return sorted(glob.glob(str(root / "alerts" / "*.yaml")))


def _count_rules(paths: list[str]) -> int:
    total = 0
    for path in paths:
        data = yaml.safe_load(pathlib.Path(path).read_text())
        total += len(_rules_of(path, data))
    return total


def main(argv: list[str]) -> int:
    paths = argv[1:] or _default_paths()
    if not paths:
        print("check_routing_labels: no alert files found", file=sys.stderr)
        return 2
    try:
        violations, waivers = check_paths(paths)
        total = _count_rules(paths)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"check_routing_labels: {exc}", file=sys.stderr)
        return 2

    if waivers:
        print(
            f"{len(waivers)} rule(s) waive a severity-keyed arm via "
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
