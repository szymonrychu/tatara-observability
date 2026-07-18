#!/usr/bin/env python3
"""Reconcile scripts/metrics_allowlist.txt against what the producer repos
actually emit - the reverse-drift gate issue #57 closes.

check_metric_provenance.py only catches ONE direction: an alert or dashboard
panel that selects a metric not on the allowlist (see that file's docstring).
It says nothing about an allowlist entry that lags a producer repo which has
REMOVED the metric: the name stays on the allowlist forever, and because
every alert file sets default_no_data_state: "OK", any rule still selecting
it reports OK / renders empty forever - the exact silent-green failure class
one level up. This script closes that hole: it shallow-clones each producer
repo at main, re-derives its emitted Prometheus metric names, and flags any
allowlist entry that no clone still emits.

DERIVATION. A metric name is recognised by anchoring on the Prometheus
constructor call itself - prometheus.NewCounterVec(...), promauto.With(reg).
NewGauge(...), prometheus.NewDesc("name", ...), prometheus.MustNewConstMetric
(...), etc. - and reading the `Name:`/`name:` field or the constructor's
positional string literal from the following few lines. The allowlist
header's documented one-liner (a bare `(Name|name): *"..."` grep) is NOT
safe run repo-wide: it also matches unrelated Go struct literals (Kubernetes
Container/Volume/Port names and the like) and produces heavy false-positive
noise. Anchoring on the constructor call eliminates that (verified against
tatara-operator and tatara-memory: 0 false positives, vs. dozens from the
bare grep - see MEMORY.md).

DIRECTIONALITY. Only the reverse direction (allowlist names no repo still
emits) is a hard failure. The forward direction (a repo emits a name that
is not on the allowlist) is reported in the job summary as informational
only and never fails the job: a component legitimately emits far more
metrics than are ever alerted on or dashboarded (confirmed: tatara-operator
alone emits ~107 names derived, only ~90 are allowlisted - the rest simply
have no rule yet, which is normal, not drift). Hard-failing that direction
would make this job permanently red on healthy state, which is exactly the
cron-fatigue failure mode issue #57's own pre-mortem warns against.

CLONE FAILURE = NEUTRAL SKIP, NEVER A HARD FAIL. A transient GitHub outage
or DNS hiccup must not block an unrelated alert/dashboard PR, and must not
turn a nightly cron run red for reasons nobody can act on. A repo that fails
to clone is skipped, with a warning, and its allowlist sections are left
unchecked for that run.

EXEMPT SECTIONS. Some allowlist sections are not derivable from any of these
repos by design and must never be diffed:
  - "external" (kube-state-metrics / kubelet metrics - not emitted by any
    tatara component).
  - claude_code_api_error_total, folded into the external section: a
    deliberately forward-looking entry for a metric that will come from a
    future OTel collector deploy (see alerts/tatara-usage-gate.yaml, rule
    "... - PENDING OTel deployment"), never from these repos' Go source.

Run: python3 scripts/reconcile_metric_provenance.py
Exit 0 = clean (including "all repos skipped"), 1 = stale allowlist entr(y/ies)
found, 2 = usage/parse error.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile

# Producer repos the allowlist's non-exempt sections are sourced from. All
# public - no clone auth needed.
REPOS: dict[str, str] = {
    "tatara-operator": "https://github.com/szymonrychu/tatara-operator.git",
    "tatara-claude-code-wrapper": "https://github.com/szymonrychu/tatara-claude-code-wrapper.git",
    "tatara-memory": "https://github.com/szymonrychu/tatara-memory.git",
    "tatara-memory-repo-ingester": "https://github.com/szymonrychu/tatara-memory-repo-ingester.git",
}

# metrics_allowlist.txt section-header key (the first word of the header
# text, see section_key()) -> which repo emits it, or None if the section is
# exempt from reconciliation (see the EXEMPT SECTIONS docstring paragraph).
# "quality" and "usage-gate" are operator-emitted sections, not separate
# repos - verified against internal/obs/operator_metrics.go and
# internal/obs/accountusage_metrics.go.
SECTION_REPO: dict[str, str | None] = {
    "operator": "tatara-operator",
    "quality": "tatara-operator",
    "usage-gate": "tatara-operator",
    "wrapper": "tatara-claude-code-wrapper",
    "memory": "tatara-memory",
    "ingester": "tatara-memory-repo-ingester",
    "external": None,
}

# Anchors a Prometheus metric constructor call.
_CTOR = re.compile(
    r"prometheus\.(?:New\w+|MustNewConstMetric)\(|promauto\.\w+\.New\w+\("
)
# The constructor's Name/name field, e.g. `Name: "operator_task_stage"`.
_NAME_FIELD = re.compile(r'(?:Name|name):\s*"([a-z][a-z0-9_]+)"')
# The constructor's positional string literal, e.g. NewDesc("operator_pushed_runs", ...).
_POSITIONAL = re.compile(r'NewDesc\(\s*"([a-z][a-z0-9_]+)"')
# How many lines after a constructor call to look for its Name field - covers
# multi-line prometheus.XOpts{...} literals.
_WINDOW = 8

_SECTION_HEADER = re.compile(r"^#\s*---\s*(.+)$")
_SECTION_KEY = re.compile(r"^([a-z][a-z-]*)")


def derive_metric_names(repo_dir: pathlib.Path) -> set[str]:
    """Every Prometheus metric name registered anywhere under repo_dir.

    Anchored on the constructor call (see module docstring) rather than a
    bare `Name: "..."` grep, so Kubernetes manifest struct literals
    (ContainerPort{Name: "http"} and the like) are never mistaken for a
    metric.
    """
    names: set[str] = set()
    for path in sorted(repo_dir.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if not _CTOR.search(line):
                continue
            window = "\n".join(lines[i : i + _WINDOW])
            m = _POSITIONAL.search(window) or _NAME_FIELD.search(window)
            if m:
                names.add(m.group(1))
    return names


def section_key(header_text: str) -> str | None:
    """The section key ("operator", "wrapper", ...) for a `# --- ... ---`
    header line's text, or None if it doesn't start with a recognised key.
    """
    m = _SECTION_KEY.match(header_text.strip().lower())
    return m.group(1) if m else None


def parse_allowlist_sections(path: pathlib.Path) -> dict[str, str]:
    """{metric_name: section_key} for every entry in metrics_allowlist.txt.

    A `# --- <text> ---` line starts a new section; its key is the leading
    word of <text> (before the first ":" or space), which stays stable even
    when the surrounding header prose changes. Multi-line header
    continuations (comment lines that don't start with "# ---") are
    comments, not new sections. Any other non-comment, non-blank line is an
    allowlist entry belonging to the current section.
    """
    out: dict[str, str] = {}
    current: str | None = None
    for line in path.read_text().splitlines():
        stripped = line.strip()
        header = _SECTION_HEADER.match(stripped)
        if header:
            current = section_key(header.group(1))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if current is not None:
            out[stripped] = current
    return out


def clone_repo(name: str, url: str, dest: pathlib.Path) -> bool:
    """Shallow-clone url@main into dest. Never raises: a clone failure is a
    neutral skip, not a hard CI failure (see module docstring)."""
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                "main",
                "--quiet",
                url,
                str(dest),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"::warning::reconcile_metric_provenance: could not clone {name} ({exc}) - "
            "skipping, treated as neutral (not a failure)",
            file=sys.stderr,
        )
        return False


def reconcile(
    entries: dict[str, str], repo_dirs: dict[str, pathlib.Path]
) -> tuple[dict[str, set[str]], dict[str, set[str]], set[str]]:
    """Diff each cloned repo's derived metric set against its allowlist
    sections. Returns (stale, new, skipped):
      stale[repo] = allowlisted names that repo no longer emits (hard-fail).
      new[repo]   = names that repo emits but no allowlist section carries
                    yet (informational only, see module docstring).
      skipped     = repo names whose clone failed and were therefore not
                    checked.
    """
    by_repo: dict[str, set[str]] = {}
    for metric, section in entries.items():
        repo = SECTION_REPO.get(section)
        if repo is None:
            continue  # exempt section - never diffed
        by_repo.setdefault(repo, set()).add(metric)

    stale: dict[str, set[str]] = {}
    new: dict[str, set[str]] = {}
    skipped: set[str] = set()
    for repo, allowlisted in by_repo.items():
        repo_dir = repo_dirs.get(repo)
        if repo_dir is None:
            skipped.add(repo)
            continue
        derived = derive_metric_names(repo_dir)
        repo_stale = allowlisted - derived
        repo_new = derived - allowlisted
        if repo_stale:
            stale[repo] = repo_stale
        if repo_new:
            new[repo] = repo_new
    return stale, new, skipped


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _write_summary(
    stale: dict[str, set[str]], new: dict[str, set[str]], skipped: set[str]
) -> None:
    import os

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["## Metric provenance reconciliation", ""]
    if stale:
        lines.append(
            "### STALE - allowlist entries no producer repo still emits (must fix)"
        )
        lines.append("")
        for repo, names in sorted(stale.items()):
            lines.append(f"**{repo}**")
            for name in sorted(names):
                lines.append(
                    f"- `{name}` - remove from `scripts/metrics_allowlist.txt` "
                    "(and repoint/delete any alert or dashboard panel still "
                    "selecting it - see check_metric_provenance.py)"
                )
        lines.append("")
    if new:
        lines.append("### New (informational only) - emitted, not yet allowlisted")
        lines.append("")
        for repo, names in sorted(new.items()):
            lines.append(f"**{repo}**: " + ", ".join(f"`{n}`" for n in sorted(names)))
        lines.append("")
    if skipped:
        lines.append(
            "### Skipped (clone failed, neutral - not checked this run): "
            + ", ".join(sorted(skipped))
        )
        lines.append("")
    if not stale and not new and not skipped:
        lines.append(
            "OK: every reconcilable allowlist section matches its producer repo."
        )
    text = "\n".join(lines) + "\n"
    print(text)
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(text)


def main(argv: list[str]) -> int:
    del argv
    allowlist_path = _root() / "scripts" / "metrics_allowlist.txt"
    try:
        entries = parse_allowlist_sections(allowlist_path)
    except OSError as exc:
        print(f"reconcile_metric_provenance: {exc}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="metric-provenance-") as tmp:
        tmp_path = pathlib.Path(tmp)
        repo_dirs: dict[str, pathlib.Path] = {}
        for name, url in REPOS.items():
            dest = tmp_path / name
            if clone_repo(name, url, dest):
                repo_dirs[name] = dest

        stale, new, skipped = reconcile(entries, repo_dirs)

    _write_summary(stale, new, skipped)

    if stale:
        total = sum(len(v) for v in stale.values())
        print(
            f"FAIL: {total} allowlist entr(y/ies) are stale - no producer repo "
            "still emits them. See the job summary above.",
            file=sys.stderr,
        )
        return 1

    print("OK: no stale allowlist entries against the repos that cloned successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
