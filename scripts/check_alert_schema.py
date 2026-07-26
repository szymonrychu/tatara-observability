#!/usr/bin/env python3
"""Fail CI when an alert YAML key is not declared in the module's variable schema.

THE SILENT-DROP TRAP. `modules/grafana_alert/variables.tf` types the module input as
`list(object({...}))`, and Terraform's object-type conversion DISCARDS any attribute the
object type does not declare. It is not an error, not a warning, not a plan diff - the key
simply never reaches Grafana. So an agent can add a perfectly spelled, perfectly valid
Grafana attribute to an alert file, watch it pass yamllint, pass lint_alert_rules.py, pass
check_metric_provenance.py, pass `terraform validate`, produce an EMPTY plan, merge, apply
green - and change nothing at all.

This is not theoretical. `keep_firing_for` (the Grafana knob that holds a rule Firing for a
grace period after its condition clears) was undeclared until 2026-07-26. A flapping
memory-stack rule (uid efraobdc2w4cgb) resolved and re-fired repeatedly, and each re-fire
minted a NEW GitHub issue - one underlying crash loop became tatara-operator #442, #444 and
#448. Setting `keep_firing_for:` in the YAML would have read as a fix and done nothing;
#82 had to work around it with a PromQL `max_over_time(...[30m])` latch instead.

So: every key used in alerts/*.yaml - at the file (rule-group) level, at the rule level, and
inside `queries[]` - must be declared in the object type in variables.tf, or be one of the
few LINT_ONLY keys below that are deliberately consumed by a checker and never rendered.
The schema is read from variables.tf itself, not restated here, so it cannot drift: declare
an attribute in the module and the key is accepted, remove it and every use fails.

Free-form maps (`annotations`, `labels`) are typed `map(string)`, so their keys are data,
not schema, and are not checked - that is where the `tatara_*` justification annotations
live.

Exit 0 = clean, 1 = undeclared key, 2 = usage/parse error (including a variables.tf this
script can no longer parse - a guard that cannot read the schema must fail loudly, not pass
everything).
"""

from __future__ import annotations

import glob
import pathlib
import re
import sys

import yaml

# Keys that are deliberately present in an alert file and deliberately NOT rendered: they
# are read by a lint script and rely on the silent drop to stay out of Grafana. Each one
# needs a CONVENTIONS.md section saying so. Keep this list tiny - it is an exemption from
# the guard above, and every entry is a key that silently does nothing in Grafana.
LINT_ONLY_KEYS = {
    # CONVENTIONS.md 6.3: file-scope justification for default_exec_err_state: Alerting,
    # read by lint_alert_rules.lint_file_exec_err_state.
    "group": frozenset({"tatara_exec_err_justification"}),
    "rule": frozenset(),
    "query": frozenset(),
}

# --- Minimal HCL type-expression reader --------------------------------------
#
# Enough of HCL to walk `variable "alerts" { type = list(object({ ... })) }` and return the
# declared attribute names per nesting level. Deliberately not a general HCL parser: it
# reads exactly the one shape this module uses, and raises if it stops recognising it.

_OBJECT = "object({"
_ATTRIBUTE = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*)\s*=")
_VARIABLE_BLOCK = re.compile(r'variable\s+"alerts"\s*\{')

_PAIRS = {"{": "}", "(": ")", "[": "]"}


def _skip_string(text: str, i: int) -> int:
    """Index just past the double-quoted string opening at text[i]."""
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        j += 1
    raise ValueError("unterminated string in variables.tf")


def _strip_comments(text: str) -> str:
    """Blank out `#`/`//` line comments, leaving strings and line structure intact."""
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            j = _skip_string(text, i)
            out.append(text[i:j])
            i = j
            continue
        if c == "#" or text.startswith("//", i):
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _match(text: str, i: int) -> int:
    """Index of the delimiter matching the opener at text[i], skipping strings."""
    opener = text[i]
    closer = _PAIRS[opener]
    depth = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unbalanced '{opener}' in variables.tf")


def _top_level_segments(body: str) -> list[str]:
    """Split an object body on commas/newlines that are not inside a nested (), [] or {}."""
    segments = []
    depth = 0
    start = 0
    i = 0
    while i < len(body):
        c = body[i]
        if c == '"':
            i = _skip_string(body, i)
            continue
        if c in _PAIRS:
            depth += 1
        elif c in _PAIRS.values():
            depth -= 1
        elif depth == 0 and c in ",\n":
            segments.append(body[start:i])
            start = i + 1
        i += 1
    segments.append(body[start:])
    return [s for s in segments if s.strip()]


def _nested(expr: str) -> dict | None:
    """The schema of the first `object({...})` inside a type expression, if any."""
    i = expr.find(_OBJECT)
    if i < 0:
        return None
    brace = i + len(_OBJECT) - 1
    return _parse_object(expr[brace + 1 : _match(expr, brace)])


def _parse_object(body: str) -> dict[str, dict | None]:
    """{attribute: nested-object-schema-or-None} for one object({...}) body."""
    schema: dict[str, dict | None] = {}
    for segment in _top_level_segments(body):
        m = _ATTRIBUTE.match(segment.strip())
        if m is None:
            continue
        schema[m.group(1)] = _nested(segment.strip()[m.end() :])
    return schema


def parse_alert_schema(path: str) -> dict[str, dict | None]:
    """Declared attributes of `variable "alerts"`, nested exactly as the type nests them."""
    text = _strip_comments(pathlib.Path(path).read_text())
    block = _VARIABLE_BLOCK.search(text)
    if block is None:
        raise ValueError(f'{path}: no `variable "alerts"` block found')
    body = text[block.end() : _match(text, block.end() - 1)]
    schema = _nested(body)
    if schema is None:
        raise ValueError(f'{path}: `variable "alerts"` declares no object({{...}}) type')
    _assert_recognisable(path, schema)
    return schema


def _assert_recognisable(path: str, schema: dict[str, dict | None]) -> None:
    """Refuse to run against a schema this parser clearly did not understand.

    A guard that silently reads an empty schema either passes everything or fails
    everything; both are worse than a loud parse error.
    """
    rules = schema.get("rules")
    if not isinstance(rules, dict) or "name" not in rules or "queries" not in rules:
        raise ValueError(
            f"{path}: parsed schema does not look like the alert module's type "
            f"(expected rules[] with name + queries, got {sorted(schema)}). "
            f"The type expression changed shape - update parse_alert_schema()."
        )


# --- The check ---------------------------------------------------------------


class Violation:
    """One YAML key that the module's object type does not declare."""

    def __init__(self, path: str, context: str, level: str, key: str, declared: list[str]):
        self.path = path
        self.context = context
        self.level = level
        self.key = key
        self.declared = declared

    def __str__(self) -> str:
        return (
            f"{self.path}: {self.context} sets `{self.key}`, which "
            f"modules/grafana_alert/variables.tf does not declare at the {self.level} level. "
            f"Terraform's object-type conversion DROPS undeclared attributes silently, so "
            f"this key never reaches Grafana and changes nothing - no error, no plan diff. "
            f"Declare it in the module (and thread it into modules/grafana_alert/main.tf) "
            f"or remove it. Declared here: {', '.join(self.declared)}."
        )


def _check_object(
    path: str,
    context: str,
    level: str,
    obj: dict,
    schema: dict[str, dict | None],
) -> list[Violation]:
    out: list[Violation] = []
    declared = sorted(schema)
    for key, value in obj.items():
        if key in LINT_ONLY_KEYS.get(level, frozenset()):
            continue
        if key not in schema:
            out.append(Violation(path, context, level, str(key), declared))
            continue
        nested = schema[key]
        if nested is None or not isinstance(value, list):
            continue
        child_level = "rule" if level == "group" else "query"
        for index, item in enumerate(value):
            if isinstance(item, dict):
                child = _child_context(context, child_level, item, index)
                out.extend(_check_object(path, child, child_level, item, nested))
    return out


def _child_context(parent: str, level: str, item: dict, index: int) -> str:
    if level == "rule":
        return f'rule "{item.get("name", "<unnamed>")}"'
    return f"{parent} query[{index}]"


def check_file(path: str, schema: dict[str, dict | None]) -> list[Violation]:
    data = yaml.safe_load(pathlib.Path(path).read_text())
    if not data or not isinstance(data, dict):
        return []
    return _check_object(path, "file", "group", data, schema)


def check_paths(paths: list[str], schema: dict[str, dict | None]) -> list[Violation]:
    out: list[Violation] = []
    for p in paths:
        out.extend(check_file(p, schema))
    return out


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    paths = argv[1:] or sorted(glob.glob(str(_root() / "alerts" / "*.yaml")))
    if not paths:
        print("check_alert_schema: no alert files found", file=sys.stderr)
        return 2
    variables_tf = str(_root() / "modules" / "grafana_alert" / "variables.tf")
    try:
        schema = parse_alert_schema(variables_tf)
        violations = check_paths(paths, schema)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"check_alert_schema: {exc}", file=sys.stderr)
        return 2
    if violations:
        print(
            f"FAIL: {len(violations)} alert key(s) are not declared in the module schema and "
            f"would be silently discarded:\n"
        )
        for v in violations:
            print(f"  - {v}")
        return 1
    print(
        f"OK: {len(paths)} alert file(s) use only keys declared in "
        f"modules/grafana_alert/variables.tf."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
