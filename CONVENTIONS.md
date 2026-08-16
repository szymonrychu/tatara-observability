# CONVENTIONS.md - tatara metric and alert classification

Normative source for how tatara services classify request/operation outcomes so
that alerts fire on real failures and stay silent on benign/transient ones. This
file lives next to `alerts/*.yaml` and the CI that enforces it
(`scripts/lint_alert_rules.py`, `.github/workflows/alert-rules-lint.yml`). The
tatara-documentation observability doc and the tatara-agent-skills review
checklist should reference this file rather than restating it.

## Why this exists

The platform's single biggest recurring operational cost has been a class of
false-positive alerts: rules and the metrics that feed them count benign or
transient conditions as real failures, so each occurrence becomes a full
incident cycle. The fix was applied four different ways across repos (producer
side, consumer side, a result taxonomy, and nowhere), so every new service
re-made the same mistake. This convention makes one classification rule and a CI
guardrail, instead of N reactive per-rule point-fixes.

## 1. Real error vs benign/transient

A **real error** is an outcome that means the service failed to do its job and a
human (or the operator incident loop) should look: an unhandled 5xx on a real
request path, a handler panic, a dependency call that errored, a job that failed.

A **benign/transient** outcome is expected under normal operation and must not,
on its own, page anyone. Known members of this class on the tatara platform:

- Readiness/liveness probe responses. `/readyz` and `/healthz` return 503 during
  a DB blip or pod boot. That is the probe doing its job, not an API error.
- Backpressure. The wrapper returns `409 "session busy"` to shed load. Expected.
- Boot-race requeues. The operator requeues during startup before caches warm.
- Expected-absent remote state. GitHub `404` on `remove_label` when the label is
  already gone.
- Incremental vs full work. An incremental ingest that does less than a full run
  is not a failure.
- Idle quantiles. `histogram_quantile` over a series with no samples yields NaN;
  an idle service is not a slow service.

## 2. The three enforcement patterns

Keep benign/transient outcomes out of the error signal using exactly one of
these, chosen per signal. Do not rely on the alert reader to remember the
exception.

1. **Producer-side exclusion (preferred for probes).** Mount probe endpoints
   OUTSIDE the request-metrics middleware so probe responses never enter
   `http_requests_total`. The metric is clean at the source; no consumer-side
   filter or per-rule exception is needed.
2. **Consumer-side filter.** If the producer still meters probes, the alert
   PromQL must exclude them in the selector, e.g.
   `route!~"/readyz|/healthz|/metrics"`.
3. **Distinguishing label.** For non-HTTP operations with expected-transient
   outcomes, carry a label that separates them, e.g. `result=ok|error|transient`,
   and alert only on `result="error"`. Never fold transient into error.

### Canonical examples in this platform

| Component | Pattern | Where |
| --- | --- | --- |
| wrapper | consumer-side filter | `alerts/tatara-wrapper.yaml` (`route!~"/readyz|/healthz|/metrics"`) |
| operator | distinguishing label | `tatara-operator/internal/obs/operator_metrics.go` (`result=ok|error|transient`) |
| memory | known gap, follow-up | `alerts/tatara-memory.yaml` (see its `tatara_probe_exclusion` annotation) |

Pattern 1 (producer-side exclusion) has no live example as of 2026-07-12: its sole
exemplar was `tatara-chat`, archived and removed from the cluster in the task-centric
redesign (its rule group, dashboard and terraform resource are deleted from this repo).
The pattern itself still stands - apply it to the next HTTP server this platform adds.

## 3. The CI lint: filter-or-justify

`scripts/lint_alert_rules.py` runs in CI on every PR that touches `alerts/**`.
For every rule whose PromQL selects a server-error status (`5..`, `5xx`, a 5xx
code, or a named 5xx status) on an `*http_requests_total` family, it requires one
of:

- a probe-route exclusion in the selector (pattern 2 above), OR
- a non-empty `tatara_probe_exclusion` annotation on the rule that explains why no
  consumer-side filter is present (pattern 1 or 3, or a documented known gap).

The annotation is a normal Grafana annotation (it renders on the alert) and reads
like:

```yaml
annotations:
  summary: "..."
  tatara_probe_exclusion: "Probes excluded producer-side: <service> mounts /readyz,/healthz outside the metrics middleware (<repo>/internal/httpapi/router.go:NN). See CONVENTIONS.md."
```

This lint is deterministic and scoped to HTTP error-ratio rules only, so it has
no false failures. It is complementary to the dark/inert-rule check (issue #18):
that one kills rules that can never fire (false negatives); this one kills rules
that fire on benign signals (false positives).

Run it locally:

```sh
pip install pyyaml
python3 scripts/lint_alert_rules.py            # lint alerts/*.yaml
python3 -m unittest discover scripts -p 'test_*.py'   # linter self-tests
```

## 4. Author checklist

When you add or change instrumentation or an alert, before opening the PR:

- Adding an HTTP server: mount `/readyz`, `/healthz`, `/metrics` OUTSIDE the
  request-metrics middleware (pattern 1). Then the metric never counts probes.
- Adding an HTTP error-ratio alert: if probes can reach the metric, add the
  selector exclusion (pattern 2). Otherwise set `tatara_probe_exclusion` citing
  where they are excluded.
- Adding an operation with expected-transient outcomes (backpressure, requeue,
  expected-absent remote state, incremental work): give it a label that
  separates transient from error (pattern 3) and alert only on the error value.
- Adding a quantile/latency alert: guard against idle NaN, e.g.
  `... and on() (sum(rate(<metric>_count[w])) > 0)`. This is linted - see 6.2.
  Then check the threshold is inside the range the quantile can actually return:
  a threshold above the histogram's top finite bucket bound can never be crossed
  and the rule reports OK forever. Also linted, against
  `scripts/histogram_bounds.txt` - see 6.4.

## 5. The CI provenance check: no alert AND NO PANEL on a metric nobody emits

`scripts/check_metric_provenance.py` runs in CI on every PR that touches
`alerts/**` or `dashboards/**`. It extracts every metric name from every
Prometheus expression - alert `queries[].expression`, dashboard
`panels[].targets[].expr` (including row-collapsed `panels[].panels[]`), and
`templating.list[]` query variables - and fails if the name is not in
`scripts/metrics_allowlist.txt`. Loki queries are skipped (alert
`query_type: loki`; a panel target whose `datasource.type` is not `prometheus`):
they select log streams, not metrics.

This kills the failure class that motivated the 2026-07-12 rewrite: every alert
file sets `default_no_data_state: "OK"`, so a rule whose metric is deleted does
not fire and does not go stale. It reports OK forever. Eight rules sat in exactly
that state, including both CD-cascade rules - the merge/deploy path to a
cluster-admin-scoped runner had zero alert coverage while every dashboard read
green.

**Dashboards are the same class, and worse** (2026-07-13): a panel on a deleted
metric renders EMPTY, SILENTLY, FOREVER, and there is not even a NoData state to
mis-configure. Two dashboards had already been declared "repointed onto the new
metric surface" while still querying seven metrics the redesign deletes.

Adding an alert on a new metric means adding the metric to
`scripts/metrics_allowlist.txt` in the same PR as the producer. That is the
point: the allowlist is the thing that forces the producer and the consumer to
move together.

**The same check also validates label VALUES**, not just metric names, for
`stageReason=`/`stage=`/`kind=`/`agent_kind=` selectors, against the closed sets
in `scripts/stage_values_allowlist.txt` (CROSS-REPO-CONTRACT F.1, F.5, A.4). A
rule can select a metric that IS emitted while filtering on a label value that
never appears in the series - same "reports OK forever" failure, one level
down, and the metric-name check alone cannot see it (fix V7-7 is the concrete
case this closes: a stale `stageReason="pod-not-ready"` reference would pass a
name-only check).

The value sweep is **metric-aware**: `kind` is an overloaded label name
(`operator_scm_writes_total{kind="write"}` is an access class, not a Task kind),
so a metric can be exempted from one label's closed set under a
`## <label>:exempt-metrics` section in `scripts/stage_values_allowlist.txt`. The
default is to CHECK - a new metric that overloads a closed-set label fails CI
until someone exempts it deliberately, with a reason.

Run it locally:

```sh
pip install pyyaml
python3 scripts/check_metric_provenance.py            # alerts/*.yaml AND dashboards/*.json
python3 -m unittest discover scripts -p 'test_*.py'    # linter self-tests (both checkers)
```

### 5.1 The third dimension: label NAMES (`check_label_provenance.py`)

A PromQL selector has three dimensions and the checks above cover two of them:
the metric NAME, and - for the four label names the value sweep happens to
hard-code - the label VALUE. The label NAME was never checked at all, and issue
#100 is what that costs. tatara-operator v2.0.0 renamed the label `stage` ->
`state` while KEEPING `operator_task_terminal_total`, so five rules selected
`operator_task_terminal_total{stage="failed"}` - a series that cannot exist -
and every check was green: the name check passed because the metric is still
emitted, and the value check passed because `stage` was one of its four known
label names and `failed` was still a member of a 28-day-stale closed set.

The trap worth naming: because `stage` appears in the value sweep's regex,
everyone read it as "`stage` is checked". Only its VALUES were.

`scripts/check_label_provenance.py` closes it. Every label NAME an expression
uses must be one the producing metric declares, and the declared set is read
out of the producer's Go source - the `[]string{...}` closing argument of the
same constructor call `reconcile_metric_provenance.py` already parses for the
name, from the same shallow clones. Not a vendored golden file: a snapshot rots
in exactly the direction this issue documents. Not live Prometheus: a labelled
vec that has never been written to has no series and would look identical to a
deleted one.

Two forms count as naming a label:

- a matcher in a selector body - `metric{label="v"}`, `=~`, `!=`, `!~`. A
  positive matcher on a label the metric does not carry matches NOTHING and the
  rule reports OK forever; a negative one matches EVERYTHING and the rule goes
  falsely NOISY. Both are the same defect;
- a `by (...)` grouping clause, but only when the expression selects exactly one
  allowlisted metric and mints no labels with `label_replace`/`label_join`.
  Grouping by an absent label is not dark - it collapses everything into one
  group with the label set to `""` - but it destroys the dimension the rule
  claims to report and blanks any `{{ index $labels "..." }}` built on it.
  `without (...)` is deliberately not checked: removing an absent label is a
  genuine no-op and defensive `without (le)` is idiomatic.

**It fails CLOSED, and that is the whole point.** This repo has shipped two
guards that reported OK when they could not see - `check_metric_provenance.py`
said OK while nine rules were dark, and `reconcile_metric_provenance.py`'s
nightly 03:23 UTC sweep sits inside #94's Grafana blackout window. So a clone
that fails all three attempts, a metric no producer declares, and a label slice
built from a variable rather than a literal are each a hard failure here, not
the neutral skip the reconcile script takes. The only exemption is the
`external` allowlist section (kube-state-metrics, kubelet, the forward-looking
OTel entry), which no tatara repo emits and which is therefore not derivable
from anything; that exemption is the `SECTION_REPO` mapping already owned by
`reconcile_metric_provenance.py`, not a second hand-maintained list, and the
count of exempted metrics is printed on every run.

Scrape-pipeline labels (`job`, `instance`, `namespace`, `pod`, `container`,
`node`, ... ) and the client library's `le`/`quantile` are legal on every metric
because the producer never declares them.

```sh
python3 scripts/check_label_provenance.py   # needs network: clones the 4 producer repos
```

## 6. Structural alert-shape checks

`scripts/lint_alert_rules.py` enforces four more conventions beyond section 3's
filter-or-justify. All four are deterministic from rule text alone (6.4 also reads
a committed provenance file, itself validated against producer source), so they
have no false failures. Each is justify-able with a named annotation, so a
deliberate exception is greppable rather than remembered.

### 6.1 No fabricated zero on a foreign exporter's metric

`or vector(0)` substitutes a literal zero when the vector is empty. On a metric
produced by a DIFFERENT exporter than the system being alerted on, paired with a
`<` (or `<=`) threshold, that turns "the exporter is unscrapeable" into "the
alerted system is down". This is tatara-observability#67: a kube-state-metrics
gap paged that the operator was down while `up{job="tatara-operator"}=1`
throughout.

The check fires when an expression contains `or vector(0)` (or `or on() vector(0)`)
AND `math_operator` is `<`/`<=` AND a `kube_*` metric appears in the expression. It only
recognises `kube_*` as a foreign exporter today - a fabricated zero on some other foreign
exporter's metric (`node_*`, `container_*`, etc.) is not currently detected.

Correct alternatives, in preference order:

1. Gate the rule on that exporter being up:
   `... < 1 and on() (up{job="kube-state-metrics"} == 1)`, plus a separate
   `absent(up{job="<exporter>"} == 1)` rule so the exporter outage itself is not a
   blind spot.
2. `absent()` / `absent_over_time()` on the series you actually care about.
3. Let `noDataState` do its job. Note that `or vector(0)` makes `noDataState`
   structurally dead code - a rule written that way can only ever be
   Normal/Alerting/Error, never NoData.

`or vector(0)` paired with a `>` threshold is the SAFE direction and is not
flagged: a fabricated zero crosses no `>` threshold. `or vector(0)` on the alerted
system's OWN `up` series is also correct and not flagged, because a vanished
self-scrape genuinely is the failure.

To keep a deliberate fabricated zero, set a non-empty `tatara_absence_fires`
annotation stating why absence must page:

```yaml
annotations:
  summary: "..."
  tatara_absence_fires: "The fabricated zero IS the condition: <reason>."
```

### 6.2 Guard every quantile against the idle NaN

`histogram_quantile` over a bucket set with no samples yields NaN. An idle
service is not a slow service (section 1's "idle quantiles" entry). This was
documented in the section 4 author checklist since 2026-07-12 and left to author
memory; it is now linted.

The check fires when an expression contains a `histogram_quantile(` call whose
own `<metric>_bucket` argument has no matching `<metric>_count ... > 0` guard
for that SAME metric family, checked independently per `histogram_quantile(`
call if an expression has more than one. The reference shape is
`alerts/tatara-operator.yaml`'s "Operator turn submit p95 latency high":

```yaml
      - expression: |
          histogram_quantile(0.95, sum(rate(<metric>_bucket{...}[15m])) by (le)) and on() (sum(rate(<metric>_count{...}[15m])) > 0)
```

The check ties the guard to the histogrammed metric's own family by name only
(text matching, not label matching) - it does not verify the guard's label
selectors match the histogram's, and a `histogram_quantile(` call whose own
arguments carry no recognisable `<metric>_bucket` selector (e.g. a recording
rule as input) is treated as unguarded rather than silently passed. Within one
`histogram_quantile(` call, only the FIRST `<metric>_bucket` selector found in
that call's own arguments is taken as its family - a second, different
`_bucket` reference later in the same call's arguments is not considered.

To keep an unguarded quantile, set a non-empty `tatara_idle_quantile` annotation
saying why that histogram is never idle.

### 6.3 No self-firing rules

`exec_err_state: Alerting` makes a rule page on its OWN query failure: a timed-out
or malformed query is reported as the condition the rule watches for. Grafana
changed this same default from `Alerting` to `Error` in 9.2.0 (PR #55345, issue
#46398) for exactly this reason. "An absent series means the system is broken" is
an argument for `no_data_state`, which is a DIFFERENT knob and can stay
`Alerting` on a genuine heartbeat.

The check fires when `exec_err_state: Alerting` is in effect - set on the rule, or
set as the file's `default_exec_err_state`. It is justified by a non-empty
`tatara_exec_err_justification` AT THE SAME SCOPE:

- rule-level setting -> a rule annotation of that name;
- file-level default -> a top-level key of that name in the alert file.

A rule that merely INHERITS an already-justified file default needs nothing extra.
A rule that opts INTO `Alerting` against an `OK`/`Error` file default needs its own
annotation. This also applies when the rule REDECLARES `exec_err_state: Alerting`
explicitly and the file default is ALREADY `Alerting` and already justified: the
rule-level check looks only at whether the rule itself sets `Alerting`, not at
whether that value happens to match the inherited default, so a redundant
re-declaration is flagged and needs its own rule-level annotation too - inheriting
(leaving `exec_err_state` unset) is the only way to ride on the file-level
justification alone.

The top-level file key is safe: Terraform's object-type conversion in
`modules/grafana_alert/variables.tf` silently drops attributes the type does not
declare, so the key never reaches Grafana and never appears in a plan.
`alerts/tatara-logs.yaml` carries the live example. That same silent drop is a
trap everywhere else - see section 7 - so this key is one of the few explicit
exemptions in `scripts/check_alert_schema.py`'s `LINT_ONLY_KEYS`.

### 6.4 A quantile threshold must be inside the range the quantile can return

Classic `histogram_quantile` returns **at most the top finite bucket bound** - a
quantile landing in the `+Inf` bucket yields that bound, never anything above it.
A rule thresholding above that ceiling cannot fire on any input, ever. It does not
go stale and it does not error either, because every alert file sets
`default_no_data_state: "OK"` and `grafana.tf` sets `default_exec_err_state = "OK"`.
It reports OK forever.

This is tatara-observability#111, and it is the same silent-green class as section
5 (an alert on a metric nobody emits) and 5.1's label dimension, one level further
down: there, the rule watched a series that did not exist; here, the series exists
and the comparison is the thing that cannot be satisfied. Two rules shipped this
way - `> 30` over a 25.6s ceiling and `> 30` over a 10s ceiling - and one of them
was cited in this file, and in the linter, as the reference example of a compliant
quantile rule. **Being correctly idle-guarded (6.2) says nothing about being
reachable.**

**The reachable set is `[q * lowest finite bound, top finite bound]`.** Prometheus's
`bucketQuantile` does interpolate the lowest bucket from 0 rather than from its own
lower bound, but it then scales by `rank/count`, and selecting that bucket bounds
`rank/count` in `[q, 1]`. So a p95 over `ExponentialBuckets(0.05, 2, 10)` can never
return below `0.95 * 0.05 = 0.0475`, and `< 0.01` on it is exactly as inert as
`> 30`. The check parses `q` out of each `histogram_quantile(` call rather than
assuming a floor of zero. A histogram whose lowest bound is `<= 0` is short-
circuited by `bucketQuantile` and returned directly, so its floor is that bound
itself.

Only a **bare** quantile is range-checked - one where the expression, after
stripping `and on() (...)` idle guards and wrapping parens, is nothing but the
`histogram_quantile(` call. A scaled or aggregated one
(`1000 * histogram_quantile(...)` for milliseconds, a comparison between two
quantiles) compares against a derived quantity in different units, and checking
those against the raw bucket range would fail a correct rule. They are skipped, and
the threshold is on the author.

Two consequences of reading the *normalised* expression rather than the raw one: a
`histogram_quantile` living INSIDE an idle guard is not range-checked, because it
contributes no value to the threshold comparison; and `or vector(N)` adds `N` to the
reachable set (it cannot lift the ceiling), so it can make a below-floor `<`
threshold legal while leaving an above-ceiling `>` threshold just as inert.

`decimal_points` is applied first. `modules/grafana_alert/main.tf` inserts a
`round($C * 10^d) / 10^d` reduce step ahead of the threshold compare, so the
ceiling the compare sees is the rounded one - rounding only ever widens it upward,
which makes `> 25.9` at `decimal_points: 0` legal over a 25.6 ceiling. The check
applies the same rounding rather than rejecting it.

Bucket ranges live in `scripts/histogram_bounds.txt`, one
`<family> <lowest bound> <top bound>` per line under a `# --- <section> ---`
header, in the shape of `metrics_allowlist.txt`. **A family with no entry there is
a hard failure, not a skip** - the check exists to make the NEXT quantile rule
safe, and a silently skipped unknown family is the bypass it was written to close.

That file is a hand-transcribed copy of a number owned by another repo, which is
exactly how `metrics_allowlist.txt` went stale in #57, and it fails in the worse
direction: a ceiling that lags a widened producer histogram makes this check reject
a threshold that has become legal - a red build on a correct rule, which is how a
check gets weakened to a warning. So it is validated, not trusted:
`scripts/reconcile_metric_provenance.py` re-derives every bound from the producer's
own `Buckets:` expression on each run and hard-fails on a mismatch. Anything it
cannot evaluate exactly - a named package-level variable, an
`append(prometheus.DefBuckets, ...)`, `ExponentialBucketsRange` - is reported as
unvalidatable and **never guessed**: a wrong derived bound is worse than an absent
one, because the mismatch message tells the author to commit the derived number.

To keep a threshold outside the derived range, set a non-empty
`tatara_histogram_range` annotation saying why it is nonetheless reachable (native
histograms enabled upstream for that family, a producer change in flight, etc.).

## 7. The CI schema check: an undeclared key is silently discarded

`modules/grafana_alert/variables.tf` types the module input as
`list(object({...}))`, and **Terraform's object-type conversion DISCARDS any
attribute the object type does not declare**. Not an error, not a warning, not a
plan diff - the key simply never reaches Grafana. So a perfectly spelled,
perfectly valid Grafana attribute added to an alert file passes yamllint, passes
`lint_alert_rules.py`, passes `check_metric_provenance.py`, passes `terraform
validate`, produces an EMPTY plan, merges, applies green, and changes nothing.

This is the third silent-green failure class in this repo, after the dark rule
(section 5) and the false-positive rule (section 3), and it is the worst of the
three because the change LOOKS applied. `keep_firing_for` - the Grafana knob that
holds a rule Firing for a grace period after its condition clears - was
undeclared until 2026-07-26. The memory stateful-member rule (uid
`efraobdc2w4cgb`) flapped, and every re-fire minted a NEW GitHub issue, so one
crash loop became tatara-operator #442, #444 and #448. Writing `keep_firing_for:`
into the alert file would have read as the fix and done nothing; PR #82 had to
work around it with a PromQL `max_over_time(...[30m])` latch instead.

`scripts/check_alert_schema.py` runs in CI on every PR that touches `alerts/**`
or `modules/grafana_alert/variables.tf`. It READS the object type out of
`variables.tf` - it does not restate it, so the two cannot drift - and fails on
any key in any `alerts/*.yaml` that the type does not declare, at the rule-group
level, the rule level, or inside `queries[]`. `annotations` and `labels` are
typed `map(string)`, so their keys are data and are not checked; that is where
the `tatara_*` justification annotations live.

Two consequences worth stating:

- **Adding a Grafana attribute is a two-file change**: declare it in
  `modules/grafana_alert/variables.tf` AND render it in
  `modules/grafana_alert/main.tf`. A declaration alone is the same no-op one
  layer down (`test_check_alert_schema.py` asserts the `keep_firing_for`
  threading for this reason).
- **A key that is deliberately lint-only** (read by a checker, never rendered)
  must be listed in `LINT_ONLY_KEYS` with a pointer to the section that defines
  it. Today that is exactly one key: section 6.3's
  `tatara_exec_err_justification`.

If the type expression in `variables.tf` ever changes shape past what the
script's reader understands, the script exits 2 with a loud parse error rather
than silently reading an empty schema - a guard that cannot read the schema must
fail, not pass everything.

Run it locally:

```sh
pip install pyyaml
python3 scripts/check_alert_schema.py                   # alerts/*.yaml keys vs the module type
python3 -m unittest discover scripts -p 'test_*.py'     # all checker self-tests
```

## 8. Every rule links to a runbook, at an anchor that provably exists

`tatara-agent-skills`' incident skill makes "follow the alert's runbook URL"
phase 2 of every incident turn on this platform. Until issue #81 that phase was
a guaranteed no-op: not one of the rules carried a `runbook_url`, so every
incident agent re-derived from scratch a fix that
`tatara-documentation/docs/operations/runbooks.md` had already published.
tatara-helmfile #245, #263 and #237 each cost a page, an incident pod and a
multi-thousand-word issue to rediscover one paragraph that page already
contained.

**Every rule carries a `runbook_url` annotation, and it is not free-form.** It
must be exactly:

```
https://szymonrychu.github.io/tatara-documentation/operations/runbooks/#tatara-runbook-<slug>
```

where `<slug>` is the rule's own `name`, lowercased, with every run of
characters outside `[a-z0-9]` collapsed to a single `-` and leading/trailing
`-` stripped. `"Wrapper commit/push failure ratio high"` becomes
`wrapper-commit-push-failure-ratio-high`.

Deriving the anchor from the rule name, rather than from a heading on the docs
page, is the point of the contract:

- A docs heading can be reworded, or its section merged into another, without
  breaking a single alert link. Only the anchor is load-bearing, and the anchor
  is not the heading.
- Neither repo keeps a mapping table, so there is nothing to drift.
- **Renaming a rule renames its anchor.** That is a deliberate, breaking change:
  add the new anchor to `docs/operations/runbooks.md` in the same change, or CI
  fails on a dangling link. Do not "fix" it by hand-editing the URL - an anchor
  that does not match its rule name is exactly the silent rot this contract
  exists to stop.

The exact-match requirement is not pedantry. The obvious way to satisfy a
weaker "is it a docs URL" check is to point forty rules at the bare `runbooks/`
page: coverage reads 100%, the incident agent follows the link, finds nothing,
and the lint now certifies the gap as closed. An exact derived match makes that
impossible to express.

`scripts/check_runbook_urls.py` enforces it, and additionally shallow-clones
tatara-documentation and asserts every anchor is declared in
`docs/operations/runbooks.md` - the same cross-repo clone pattern as
`reconcile_metric_provenance.py`, and a neutral skip on clone failure for the
same reason. An anchor may be backed by a written runbook (`status: covered`)
or by an honest "no runbook yet" placeholder (`status: none`); both resolve, and
the covered/total split is printed into the job summary on every run so runbook
coverage is a number rather than a guess.

The reverse direction - "an anchor was silently removed or renamed on the docs
page" - is guarded in tatara-documentation by
`scripts/check_runbook_anchors.py`, not here, so the break is reported in the
PR that causes it rather than against an unrelated alerts PR.

Run it locally:

```sh
python3 scripts/check_runbook_urls.py     # needs network for the anchor half
```
