#!/usr/bin/env python3
"""Reconcile scripts/metrics_allowlist.txt and scripts/histogram_bounds.txt
against what the producer repos actually emit - the reverse-drift gate issue #57
closes, extended in #111 to the bucket ceilings.

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

BUCKET CEILINGS (#111). The same clones also validate scripts/histogram_bounds.txt,
which feeds lint_alert_rules.py Check 4 (a threshold outside the range a
histogram_quantile can return makes a rule structurally inert). That file is a
hand-transcribed copy of a number owned by another repo - the same drift class as
the allowlist - so every bound is re-derived from the producer's own `Buckets:`
expression and a MISMATCH IS A HARD FAIL. This direction is failed, unlike the
`new` direction above, because a ceiling that lags a widened producer histogram
makes the linter reject a threshold that has become legal. See derive_bucket_bounds
and reconcile_bounds.

Run: python3 scripts/reconcile_metric_provenance.py
Exit 0 = clean (including "all repos skipped"), 1 = stale allowlist entr(y/ies) or
drifted bucket ceiling(s) found, 2 = usage/parse error.
"""

from __future__ import annotations

import math
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import NamedTuple

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


# --- histogram bucket ceilings (tatara-observability#111) -------------------
#
# scripts/histogram_bounds.txt feeds lint_alert_rules.py Check 4, which rejects an
# alert thresholding a histogram_quantile() outside the range that quantile can
# return. That file is a hand-transcribed copy of a number owned by another repo -
# exactly the drift class that made metrics_allowlist.txt stale in #57 - and it
# fails in the WORST direction: a ceiling that lags a widened producer histogram
# makes the linter reject a threshold that has become legal, which is a red build
# on a correct rule, which is how a check gets weakened to a warning. So every
# bound is re-derived here from the producer's own source and a mismatch is a HARD
# FAIL, unlike the informational forward direction above.
#
# Underivable is NOT a failure and never a guess: a `Buckets:` field behind a named
# package-level variable (tatara-memory's analyticsDurationBuckets /
# requestDurationBuckets) or absent entirely is reported and moved past.
# Matches every NewHistogram form, including the promauto ones _CTOR's
# `promauto.<ident>.` shape misses (promauto.NewHistogramVec,
# promauto.With(reg).NewHistogramVec).
_HISTOGRAM_CTOR = re.compile(r"\bNewHistogram(?:Vec)?\(")
_GO_NUMBER = r"[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][-+]?[0-9]+)?"
_EXPONENTIAL_BUCKETS = re.compile(
    rf"^(?:\w+\.)?ExponentialBuckets\(\s*({_GO_NUMBER})\s*,\s*({_GO_NUMBER})\s*,\s*({_GO_NUMBER})\s*\)$"
)
_LINEAR_BUCKETS = re.compile(
    rf"^(?:\w+\.)?LinearBuckets\(\s*({_GO_NUMBER})\s*,\s*({_GO_NUMBER})\s*,\s*({_GO_NUMBER})\s*\)$"
)
_DEF_BUCKETS = re.compile(r"^(?:\w+\.)?DefBuckets$")
_LITERAL_BUCKETS = re.compile(r"^\[\]float64\{([^}]*)\}$")
_BUCKETS_FIELD = re.compile(r"\bBuckets:\s*")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_GO_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_GO_RAW_STRING = re.compile(r"`[^`]*`", re.S)
# prometheus.DefBuckets = {.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10}.
_DEF_BUCKETS_RANGE = (0.005, 10.0)
# How many lines after a histogram constructor to look for its Buckets field. Wider
# than _WINDOW because Help strings push Buckets further down, and truncated at the
# next constructor so one declaration never adopts the next one's ladder.
_BUCKET_WINDOW = 12


def _go_float(text: str) -> float:
    return float(text.replace("_", ""))


def _buckets_expression(window: str) -> str | None:
    """The Go expression assigned to the `Buckets:` field in window, or None.

    Comments (line AND block) and string literals (interpreted AND raw) are
    blanked first, so a ladder quoted in a Help string or left behind in a
    `// was Buckets: []float64{...}` comment cannot be mistaken for the live
    value. Strings go first so a `//` inside one is not read as a comment. The
    expression is then read by balancing brackets from the field, so only the
    value itself is returned - never a neighbouring declaration's.
    """
    window = _GO_RAW_STRING.sub("``", _GO_STRING.sub('""', window))
    window = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", window))
    m = _BUCKETS_FIELD.search(window)
    if m is None:
        return None
    depth = 0
    for i in range(m.end(), len(window)):
        ch = window[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return window[m.end() : i].strip()
            depth -= 1
        elif depth == 0 and (ch == "," or ch == "\n"):
            return window[m.end() : i].strip()
    return window[m.end() :].strip()


def _bucket_range(window: str) -> tuple[float, float] | None:
    """(lowest, top) finite bucket bound for the `Buckets:` expression in window,
    or None when the expression is not one this parser evaluates exactly.

    Anything not recognised - a named package-level variable, an
    `append(prometheus.DefBuckets, ...)`, ExponentialBucketsRange, a computed
    slice - returns None and is reported as unvalidatable. NEVER guessed: a wrong
    derived bound is worse than an absent one, because a mismatch is a hard CI
    failure whose message tells the author to commit the derived number.
    """
    expr = _buckets_expression(window)
    if not expr:
        return None
    m = _EXPONENTIAL_BUCKETS.match(expr)
    if m:
        start, factor, count = (_go_float(g) for g in m.groups())
        return start, start * factor ** (int(count) - 1)
    m = _LINEAR_BUCKETS.match(expr)
    if m:
        start, width, count = (_go_float(g) for g in m.groups())
        return start, start + width * (int(count) - 1)
    m = _LITERAL_BUCKETS.match(expr)
    if m:
        values = [v.strip() for v in m.group(1).split(",") if v.strip()]
        try:
            floats = [_go_float(v) for v in values]
        except ValueError:
            return None
        return (min(floats), max(floats)) if floats else None
    if _DEF_BUCKETS.match(expr):
        return _DEF_BUCKETS_RANGE
    return None


def derive_bucket_bounds(repo_dir: pathlib.Path) -> dict[str, tuple[float, float]]:
    """{metric family: (lowest, top) finite bucket bound} for every histogram
    under repo_dir whose Buckets expression this parser can evaluate exactly."""
    bounds: dict[str, tuple[float, float]] = {}
    for path in sorted(repo_dir.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if not _HISTOGRAM_CTOR.search(line):
                continue
            end = min(i + _BUCKET_WINDOW, len(lines))
            for j in range(i + 1, end):
                if _CTOR.search(lines[j]) or _HISTOGRAM_CTOR.search(lines[j]):
                    end = j
                    break
            window = "\n".join(lines[i:end])
            name = _NAME_FIELD.search(window)
            if not name:
                continue
            rng = _bucket_range(window)
            if rng is not None:
                bounds[name.group(1)] = rng
    return bounds


_BOUNDS_ENTRY = re.compile(r"^([a-z][a-z0-9_]*)\s+(\S+)\s+(\S+)$")


def parse_histogram_bounds(
    path: pathlib.Path,
) -> dict[str, tuple[str, float, float]]:
    """{family: (section_key, lowest bound, top bound)} from histogram_bounds.txt.
    Same `# --- <section> ---` routing as parse_allowlist_sections."""
    out: dict[str, tuple[str, float, float]] = {}
    current: str | None = None
    for line in path.read_text().splitlines():
        stripped = line.strip()
        header = _SECTION_HEADER.match(stripped)
        if header:
            current = section_key(header.group(1))
            continue
        if not stripped or stripped.startswith("#") or current is None:
            continue
        m = _BOUNDS_ENTRY.match(stripped)
        if not m:
            continue
        try:
            out[m.group(1)] = (current, float(m.group(2)), float(m.group(3)))
        except ValueError as exc:
            raise ValueError(f"histogram_bounds.txt: bad entry {stripped!r}") from exc
    return out


class BoundsReconciliation(NamedTuple):
    """The four diff directions of histogram_bounds.txt vs. the producer source,
    plus the repos that could not be read.

    mismatched[repo][family] = (file range, derived range) - HARD FAIL.
    missing[repo][family]    = derived range for a histogram the file omits
                               (informational, same direction as `new` above).
    unvalidatable            = families the producer STILL declares but whose
                               Buckets: expression this parser cannot evaluate
                               (a named package-level var) - reported, never failed.
    ghost                    = families the producer no longer declares AT ALL, so
                               the committed bound describes a series that does not
                               exist - HARD FAIL. This is the `stale` allowlist case
                               one level down, and it is the direction
                               histogram_bounds.txt's own header promises cannot go
                               stale unnoticed. It must not be folded into
                               unvalidatable: 7 of the bounds families are not on
                               metrics_allowlist.txt, so the stale gate does not
                               backstop them.
    skipped                  = repos whose clone failed.
    """

    mismatched: dict[str, dict[str, tuple[tuple[float, float], tuple[float, float]]]]
    missing: dict[str, dict[str, tuple[float, float]]]
    unvalidatable: set[str]
    ghost: set[str]
    skipped: set[str]


def reconcile_bounds(
    entries: dict[str, tuple[str, float, float]], repo_dirs: dict[str, pathlib.Path]
) -> BoundsReconciliation:
    """Diff histogram_bounds.txt against each producer's derived bucket ranges."""
    by_repo: dict[str, dict[str, tuple[float, float]]] = {}
    for family, (section, low, high) in entries.items():
        repo = SECTION_REPO.get(section)
        if repo is None:
            continue  # exempt section - never diffed
        by_repo.setdefault(repo, {})[family] = (low, high)

    mismatched: dict[str, dict[str, tuple[tuple[float, float], tuple[float, float]]]] = {}
    missing: dict[str, dict[str, tuple[float, float]]] = {}
    unvalidatable: set[str] = set()
    ghost: set[str] = set()
    skipped: set[str] = set()
    for repo, filed in by_repo.items():
        repo_dir = repo_dirs.get(repo)
        if repo_dir is None:
            skipped.add(repo)
            continue
        derived = derive_bucket_bounds(repo_dir)
        declared = derive_metric_names(repo_dir)
        for family, rng in filed.items():
            if family not in derived:
                # The bound could not be re-derived. WHY decides the outcome: a
                # histogram the producer still declares is the benign named-var
                # case; one it no longer declares at all is a ghost.
                (unvalidatable if family in declared else ghost).add(family)
            elif not all(
                math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-12)
                for a, b in zip(rng, derived[family])
            ):
                mismatched.setdefault(repo, {})[family] = (rng, derived[family])
        absent = {f: r for f, r in derived.items() if f not in filed}
        if absent:
            missing[repo] = absent
    return BoundsReconciliation(mismatched, missing, unvalidatable, ghost, skipped)


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
    stale: dict[str, set[str]],
    new: dict[str, set[str]],
    skipped: set[str],
    mismatched: dict[str, dict[str, tuple[float, float]]],
    missing: dict[str, dict[str, float]],
    unvalidatable: set[str],
    ghost: set[str],
) -> None:
    import os

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["## Metric provenance reconciliation", ""]
    if mismatched:
        lines.append(
            "### DRIFTED - histogram_bounds.txt disagrees with the producer's source (must fix)"
        )
        lines.append("")
        for repo, families in sorted(mismatched.items()):
            lines.append(f"**{repo}**")
            for family, (filed, derived) in sorted(families.items()):
                lines.append(
                    f"- `{family}`: `scripts/histogram_bounds.txt` says "
                    f"{filed[0]:g}..{filed[1]:g}, the producer's buckets run "
                    f"{derived[0]:g}..{derived[1]:g}. Update the file - a stale range "
                    "makes lint_alert_rules.py Check 4 reject a threshold that is now "
                    "legal."
                )
        lines.append("")
    if ghost:
        lines.append(
            "### GHOST - bucket-range entries for a histogram no producer still "
            "declares (must fix)"
        )
        lines.append("")
        for family in sorted(ghost):
            lines.append(
                f"- `{family}` - the producer repo no longer declares this histogram "
                "at all, so the committed bound describes a series that does not "
                "exist. Remove it from `scripts/histogram_bounds.txt` (and repoint "
                "any alert or panel still selecting it)."
            )
        lines.append("")
    if missing:
        lines.append(
            "### Histograms with no bucket-range entry (informational only)"
        )
        lines.append("")
        for repo, families in sorted(missing.items()):
            lines.append(
                f"**{repo}**: "
                + ", ".join(
                    f"`{f}` ({r[0]:g}..{r[1]:g})" for f, r in sorted(families.items())
                )
            )
        lines.append("")
    if unvalidatable:
        lines.append(
            "### Unvalidatable bucket ceilings (reported, never failed): "
            + ", ".join(f"`{f}`" for f in sorted(unvalidatable))
            + " - the producer's `Buckets:` is a named variable or absent, so the "
            "committed bound cannot be re-derived - but the histogram itself is "
            "still declared, so the entry is live. Verify the number by hand."
        )
        lines.append("")
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
    if not any((stale, new, skipped, mismatched, missing, unvalidatable, ghost)):
        lines.append(
            "OK: every reconcilable allowlist section matches its producer repo, "
            "and every histogram bucket ceiling matches its producer's source."
        )
    text = "\n".join(lines) + "\n"
    print(text)
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(text)


def main(argv: list[str]) -> int:
    del argv
    allowlist_path = _root() / "scripts" / "metrics_allowlist.txt"
    bounds_path = _root() / "scripts" / "histogram_bounds.txt"
    try:
        entries = parse_allowlist_sections(allowlist_path)
        bounds_entries = parse_histogram_bounds(bounds_path)
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
        bounds = reconcile_bounds(bounds_entries, repo_dirs)

    # bounds.skipped is deliberately not merged into `skipped`: both come from the
    # same repo_dirs, so the allowlist path above already reports every repo whose
    # clone failed, and reporting it twice would read as two separate failures.
    _write_summary(
        stale,
        new,
        skipped,
        bounds.mismatched,
        bounds.missing,
        bounds.unvalidatable,
        bounds.ghost,
    )

    if stale:
        total = sum(len(v) for v in stale.values())
        print(
            f"FAIL: {total} allowlist entr(y/ies) are stale - no producer repo "
            "still emits them. See the job summary above.",
            file=sys.stderr,
        )
        return 1

    if bounds.mismatched:
        total = sum(len(v) for v in bounds.mismatched.values())
        print(
            f"FAIL: {total} histogram bucket ceiling(s) in scripts/histogram_bounds.txt "
            "disagree with their producer's source. See the job summary above.",
            file=sys.stderr,
        )
        return 1

    if bounds.ghost:
        print(
            f"FAIL: {len(bounds.ghost)} bucket-range entr(y/ies) in "
            "scripts/histogram_bounds.txt name a histogram no producer repo still "
            "declares. See the job summary above.",
            file=sys.stderr,
        )
        return 1

    print(
        "OK: no stale allowlist entries and no drifted bucket ceilings against the "
        "repos that cloned successfully."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
