#!/usr/bin/env python3
"""Fail CI when an alert rule has no runbook_url, or when its runbook_url points at an
anchor tatara-documentation does not declare.

WHY. `tatara-agent-skills/skills/investigation/tatara-incident-sre/SKILL.md` makes
"follow the alert's runbookURL" phase 2 of every incident turn on this platform. With no
`runbook_url` on any rule that phase was a guaranteed no-op, and incident agents
re-derived from scratch fixes that were already written in
tatara-documentation/docs/operations/runbooks.md (tatara-helmfile #245, #263 and #237 all
rediscovered a paragraph that page already contained). This check makes the annotation
mandatory and makes the link verifiable.

THE ANCHOR CONTRACT. The link target is NOT free-form. Every rule's runbook_url must be
exactly:

    https://szymonrychu.github.io/tatara-documentation/operations/runbooks/#tatara-runbook-<slug>

where <slug> is the rule's own `name`, lowercased, with every run of characters outside
[a-z0-9] collapsed to a single "-" and leading/trailing "-" stripped. Deriving the anchor
from the rule name rather than from a docs heading is the whole point: a docs heading can
be reworded freely and the link still resolves, and neither side needs a hand-maintained
mapping table that can silently drift. The contract is documented for authors in
CONVENTIONS.md section 8 and on the docs page itself.

Requiring the EXACT derived URL, not merely "some URL under the docs site", is deliberate.
The pre-mortem failure mode for a hard gate is an author satisfying it by pointing forty
rules at the bare `runbooks/` page: coverage would read 100%, the incident agent would
follow the link, find nothing, and the lint would now CERTIFY the gap as closed. An exact
match makes that impossible to express.

THE CROSS-REPO HALF. A well-formed URL still resolves to nothing if the docs page never
declares that anchor. This script shallow-clones tatara-documentation at main and asserts
every anchor exists in docs/operations/runbooks.md, mirroring
reconcile_metric_provenance.py, which already shallow-clones 4 producer repos in this same
workflow. It also reports how many of those anchors are backed by a real runbook versus an
honest "no runbook yet" placeholder, so runbook coverage is a number in the job summary
rather than a guess.

DIRECTIONALITY. This repo owns the alert list, so "an alert with no anchor" can only be
detected here and is a hard failure here. tatara-documentation owns the anchor set, so
"an anchor was silently removed or renamed" is detected THERE, by
scripts/check_runbook_anchors.py, in the PR that causes it. Neither check can be moved to
the other repo without reporting the break to an author who did not cause it.

CLONE FAILURE = NEUTRAL SKIP, NEVER A HARD FAIL. Same rule as
reconcile_metric_provenance.py: a GitHub blip must not block an unrelated alerts PR. The
local checks (present, well-formed, exact) still run and still fail; only the anchor
existence check is skipped.

CROSS-REPO ORDERING. A change that adds a rule and its runbook is two PRs, and the docs
one has to land first or this check sees an anchor that does not exist yet. Set
TATARA_DOCS_REF to the docs branch to validate the pair before either merges:

    TATARA_DOCS_REF=feat/my-runbook python3 scripts/check_runbook_urls.py

It defaults to main, so CI always checks against what is actually published.

Run: python3 scripts/check_runbook_urls.py
Exit 0 = clean (including "clone skipped"), 1 = violations, 2 = usage/parse error.
"""

from __future__ import annotations

import glob
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

ANNOTATION_KEY = "runbook_url"

DOCS_REPO = "https://github.com/szymonrychu/tatara-documentation.git"
RUNBOOKS_PATH = "docs/operations/runbooks.md"
RUNBOOK_BASE = (
    "https://szymonrychu.github.io/tatara-documentation/operations/runbooks/#"
)
ANCHOR_PREFIX = "tatara-runbook-"

# One declared anchor on the docs page. Both halves are required: the anchor element the
# browser jumps to, and the machine-readable marker naming the alert it serves and whether
# a real runbook backs it. See tatara-documentation/scripts/check_runbook_anchors.py, which
# is the authority on this format.
_DECLARED_ANCHOR = re.compile(
    r'<a id="(?P<id>tatara-runbook-[a-z0-9-]+)"></a>'
    r'<!-- alert: "(?P<alert>[^"]+)" status: (?P<status>covered|none) -->'
)

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(rule_name: str) -> str:
    """The anchor slug for a rule name. See the module docstring for the contract."""
    return re.sub(r"-+", "-", _NON_SLUG.sub("-", rule_name.lower())).strip("-")


def expected_url(rule_name: str) -> str:
    return RUNBOOK_BASE + ANCHOR_PREFIX + slugify(rule_name)


class Violation:
    """One rule whose runbook_url is missing, malformed, or dangling."""

    def __init__(self, path: str, rule: str, message: str):
        self.path = path
        self.rule = rule
        self.message = message

    def __str__(self) -> str:
        return f'{self.path}: rule "{self.rule}" {self.message}'


def check_rule(path: str, rule: dict) -> Violation | None:
    """The local half: present, and exactly the URL the contract derives."""
    name = rule.get("name", "<unnamed>")
    url = str((rule.get("annotations") or {}).get(ANNOTATION_KEY, "")).strip()
    want = expected_url(name)
    if not url:
        return Violation(
            path,
            name,
            f"has no `{ANNOTATION_KEY}` annotation. Every rule needs one, and it must be "
            f'exactly "{want}" (the anchor is derived from the rule name - see '
            "CONVENTIONS.md section 8).",
        )
    if url != want:
        return Violation(
            path,
            name,
            f'has `{ANNOTATION_KEY}: "{url}"` but the contract derives '
            f'"{want}" from its name. The anchor is NOT free-form: pointing at the bare '
            "runbooks page, or at a hand-picked heading slug, would satisfy a weaker "
            "check while resolving to nothing useful. Rename the anchor on the docs page "
            "rather than the URL here.",
        )
    return None


def check_paths(paths: list[str]) -> tuple[list[Violation], dict[str, str]]:
    """Run the local check over every rule. Returns (violations, {anchor: rule name})."""
    violations: list[Violation] = []
    anchors: dict[str, str] = {}
    for path in paths:
        data = yaml.safe_load(pathlib.Path(path).read_text())
        if not data or not isinstance(data, dict):
            continue
        for rule in data.get("rules") or []:
            v = check_rule(path, rule)
            if v is not None:
                violations.append(v)
                continue
            anchors[ANCHOR_PREFIX + slugify(rule["name"])] = rule["name"]
    return violations, anchors


def strip_code_fences(markdown: str) -> str:
    """Drop fenced code blocks. runbooks.md documents the anchor format with a worked
    example inside a fence; an example is not a declaration, and counting one would let a
    dangling link be "satisfied" by an anchor no browser ever renders. Mirrors
    tatara-documentation/scripts/check_runbook_anchors.py:strip_code_fences."""
    out, in_fence = [], False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def parse_declared_anchors(markdown: str) -> dict[str, str]:
    """{anchor id: status} for every anchor runbooks.md declares."""
    return {
        m.group("id"): m.group("status")
        for m in _DECLARED_ANCHOR.finditer(strip_code_fences(markdown))
    }


def docs_ref() -> str:
    """The tatara-documentation ref to check anchors against. main unless overridden."""
    return os.environ.get("TATARA_DOCS_REF", "").strip() or "main"


def clone_docs(dest: pathlib.Path, ref: str) -> bool:
    """Shallow-clone tatara-documentation@ref into dest. Never raises: a clone failure is
    a neutral skip, not a hard CI failure (see module docstring)."""
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                ref,
                "--quiet",
                DOCS_REPO,
                str(dest),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"::warning::check_runbook_urls: could not clone tatara-documentation@{ref} "
            f"({exc}) - skipping the anchor-existence check, treated as neutral (not a "
            "failure)",
            file=sys.stderr,
        )
        return False


def reconcile(anchors: dict[str, str], declared: dict[str, str]) -> list[str]:
    """Anchors this repo links to that the docs page does not declare."""
    return sorted(a for a in anchors if a not in declared)


def _write_summary(
    anchors: dict[str, str], declared: dict[str, str] | None, dangling: list[str]
) -> None:
    lines = ["## Alert runbook links", ""]
    lines.append(f"{len(anchors)} alert rule(s) carry a well-formed `runbook_url`.")
    lines.append("")
    if declared is None:
        lines.append(
            "Anchor existence NOT checked this run (tatara-documentation clone failed - "
            "neutral skip)."
        )
    else:
        backed = sorted(a for a in anchors if declared.get(a) == "covered")
        placeholder = sorted(a for a in anchors if declared.get(a) == "none")
        lines.append(
            f"Runbook coverage: **{len(backed)}/{len(anchors)}** alerts have a written "
            f"runbook; {len(placeholder)} resolve to an honest "
            "`no runbook yet` placeholder."
        )
        if placeholder:
            lines.append("")
            lines.append("Placeholder (no runbook written yet):")
            for a in placeholder:
                lines.append(f"- `{anchors[a]}`")
        if dangling:
            lines.append("")
            lines.append(
                "### DANGLING - anchor not declared in docs/operations/runbooks.md"
            )
            lines.append("")
            for a in dangling:
                lines.append(
                    f"- `{a}` (rule `{anchors[a]}`) - add the anchor to "
                    "tatara-documentation `docs/operations/runbooks.md`, or rename the "
                    "rule so the derived anchor matches an existing one."
                )
    text = "\n".join(lines) + "\n"
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(text)


def _default_paths() -> list[str]:
    root = pathlib.Path(__file__).resolve().parent.parent
    return sorted(glob.glob(str(root / "alerts" / "*.yaml")))


def main(argv: list[str]) -> int:
    paths = argv[1:] or _default_paths()
    if not paths:
        print("check_runbook_urls: no alert files found", file=sys.stderr)
        return 2
    try:
        violations, anchors = check_paths(paths)
    except (OSError, yaml.YAMLError) as exc:
        print(f"check_runbook_urls: {exc}", file=sys.stderr)
        return 2

    if violations:
        print(
            f"FAIL: {len(violations)} alert rule(s) have a missing or non-contract "
            f"`{ANNOTATION_KEY}`:\n"
        )
        for v in violations:
            print(f"  - {v}")
        print("\nSee CONVENTIONS.md section 8 for the anchor contract.")
        return 1

    declared: dict[str, str] | None = None
    ref = docs_ref()
    with tempfile.TemporaryDirectory(prefix="runbook-anchors-") as tmp:
        dest = pathlib.Path(tmp) / "tatara-documentation"
        if clone_docs(dest, ref):
            try:
                declared = parse_declared_anchors((dest / RUNBOOKS_PATH).read_text())
            except OSError as exc:
                print(
                    f"::warning::check_runbook_urls: cloned tatara-documentation but could "
                    f"not read {RUNBOOKS_PATH} ({exc}) - skipping the anchor-existence check",
                    file=sys.stderr,
                )

    dangling = reconcile(anchors, declared) if declared is not None else []
    _write_summary(anchors, declared, dangling)

    if dangling:
        print(
            f"FAIL: {len(dangling)} runbook_url anchor(s) are not declared in "
            f"tatara-documentation@{ref} {RUNBOOKS_PATH}. See the job summary above. If the "
            "companion docs PR has not merged yet, that is this check working: land it "
            "first, or re-run with TATARA_DOCS_REF set to its branch.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {len(anchors)} alert rule(s) carry a contract-shaped, resolvable runbook_url."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
