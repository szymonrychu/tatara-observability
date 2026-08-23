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
- Adding or repointing the dashboard panel that backs such an alert: the panel's
  red threshold step is under the same ceiling, and its `description` quotes the
  alert's real threshold. Linted - see 6.5.

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

`scripts/lint_alert_rules.py` enforces five more conventions beyond section 3's
filter-or-justify. All are deterministic from rule (or panel) text alone (6.4 and
6.5 also read a committed provenance file, itself validated against producer
source), so they have no false failures. Each rule-level one is justify-able with a
named annotation, so a deliberate exception is greppable rather than remembered;
6.5 runs over `dashboards/*.json`, which carry no annotations, and is narrowed
instead.

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
quantiles, a rule splitting its quantile across several `queries`) compares against
a derived quantity in different units, and checking those against the raw bucket
range would fail a correct rule. Those are not range-checked - but the carve-out is
**declared, not silent**: such a rule must set `tatara_histogram_range` saying why
its threshold is reachable, so every quantile rule this check does not verify is
one `grep` away. A skip nobody can enumerate is the same bypass as a silently
skipped unknown family.

Normalisation truncates the expression at its first top-level `and`/`unless`. Those
are set FILTERS - `A and B` yields A's samples - so the value a threshold sees is
always the left operand, and `and` binds looser than every arithmetic and comparison
operator. Two consequences: a `histogram_quantile` on the RIGHT of an `and` (an idle
guard, or a latency filter on a non-latency rule) is not range-checked and does not
need declaring, because it is not the value; and a guard written without wrapping
parens, or written before the quantile, is normalised correctly rather than dropping
the rule out of the check. Matching the guard by SHAPE did drop it, which is how a
check written to close a bypass grows one.

`or vector(N)` adds `N` to the reachable set (it cannot lift the ceiling), so it can
make a below-floor `<` threshold legal while leaving an above-ceiling `>` threshold
just as inert. It is extracted before the `and` truncation, since `or` binds looser
still.

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
"Unvalidatable" means the producer still declares the histogram and only its
`Buckets:` expression is opaque - and the evidence for "still declared" is read with
the same window width as the bucket parser, so a `Name:` field pushed down by a long
`Help` string cannot masquerade as a deletion. An entry naming a histogram no
producer declares at all is a **ghost** and is a hard failure - that is section 5's stale-allowlist
direction one level down, and `metrics_allowlist.txt` does not backstop it, because
7 of the bounds families are not on it.

To keep a threshold outside the derived range, set a non-empty
`tatara_histogram_range` annotation saying why it is nonetheless reachable (native
histograms enabled upstream for that family, a producer change in flight, etc.).

### 6.5 A dashboard threshold step must be inside that range too

Check 4 walks `alerts/*.yaml`. The identical defect was live in `dashboards/` the
entire time it was being written: `operator.json` and `memory.json` both painted a
red threshold step at 30 over the same two histograms, and their panel descriptions
told a responder to expect it. A Grafana step colours a value at `value >= step`,
so a step above the top finite bucket bound never colours - and unlike an alert
there is not even a `no_data_state` to mis-configure. Section 5 already treats
`dashboards/*.json` as a first-class silent-green surface; this is the comparison
dimension of it.

`lint_dashboard_file` range-checks a panel only when it carries at least one finite
**absolute** threshold step AND every one of its Prometheus targets is a bare
quantile, so the step is in the histogram's own units. The ceiling is the highest
across the panel's targets, and the verdict is computed from all of them before
anything is reported, so it cannot depend on target order. An unknown family is a
hard failure, as in 6.4.

Steps are read from `fieldConfig.defaults.thresholds` AND from per-series
`fieldConfig.overrides[].properties[].id == "thresholds"` - an idiom
`dashboards/task-delivery.json` already uses, and a red step hidden in an override
is exactly as unreachable as one in the defaults. A numeric string step (`"30"`) is
a step; Grafana coerces it.

Four deliberate narrowings, each of which is a false NEGATIVE traded for zero false
failures:

- A panel mixing a quantile with a non-quantile target, or scaling one into
  milliseconds, is skipped. A step applies to every series in the panel, so one
  out-of-model series leaves the panel's maximum unbounded and nothing can be
  concluded. This means adding a throughput overlay to either of the two panels this
  check exists for turns it off. Unlike a rule, a panel has no annotations, so there
  is no per-panel escape hatch to declare - this paragraph is the record instead.
- `thresholds.mode: "percentage"` is skipped: a step is then a percentage of the
  field's min..max, not a value in the metric's units.
- `custom.thresholdsStyle.mode: "off"` is skipped: the band is never drawn, so a
  leftover step has no rendered effect. Most timeseries panels here set it.
- Only the unreachable direction is failed. A step at or below the floor paints a
  permanently-red band, which is a different defect and is not modelled.

`decimal_points` has no analogue here: `fieldConfig.decimals` is display formatting
and does not round the value a step is evaluated against.

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

`TATARA_DOCS_REF` is a **union over `main`, never a replacement for it.** A
change that adds a rule and its runbook is two PRs, and the docs one has to
land first or this check sees an anchor that does not exist yet:

```sh
TATARA_DOCS_REF=feat/my-runbook python3 scripts/check_runbook_urls.py
```

`main` is always cloned and always authoritative; the ref may only **add**
anchors on top of it. Replacement was the original behaviour and it silently
narrowed the check - pointing a run at a docs branch stopped validating against
what is published, so an anchor *deleted* on that branch read as clean.
Anchors that resolved only via the ref are listed in the job summary, because
they are unpublished: merging this repo before the docs PR is exactly what
leaves those links dangling. CI passes `github.head_ref`, which is empty on
push, so a merge to `main` is always checked against published `main` alone.

## 9. The chart file is a specification, and CI reconciles it

Until 2026-08-23 this platform had **two alerting planes and delivered on one.**
`tatara-operator`'s chart ships a 32-alert `PrometheusRule` with
`prometheusRule.enabled: true`, and this cluster's Prometheus never loaded a
single one: the kube-prometheus-stack `ruleSelector` matches
`release=prometheus` and `tatara-helmfile` never set
`prometheusRule.additionalLabels`.

Setting that label is the obvious fix and is the wrong one. The cluster
Alertmanager is stock kube-prometheus-stack: `route.receiver: "null"`, one
`alertname="Watchdog"` child route, and **one receiver named `"null"` carrying
zero integrations.** It was discarding `PvcBackupJobFailed` on 11 series at the
time this was measured and nobody had noticed. Labelling the rule would have
loaded 32 alerts that evaluate, fire and are dropped - manufactured coverage,
which is worse than the silence it replaced. Grafana is the plane that
delivers, because the `Tatara` contact point webhooks
`/operator/webhooks/<project>/grafana`, and that is the only incident-Task
minting path.

So `tatara-helmfile` sets `prometheusRule.enabled: false` and this repo is the
single alerting plane on this cluster. That gives the chart file a new job:
**it is the specification of conditions the producer thinks are worth alerting
on, and `alerts/` is what is actually alerted.**

What that split costs when nothing reconciles it is already on the record.
`tatara-operator#635` filed `TataraAccountUsageFeedDead` as "never written" on
the strength of a grep against this repo. It *was* written - in the plane that
delivered nothing. **A competent audit of the wrong plane is indistinguishable
from a real gap.**

`scripts/check_chart_alert_parity.py` is the reconciliation. Every metric a
chart alert reads must be read by some rule in `alerts/`. It is deliberately a
metric-level check, not a rule-level one: thresholds, groupings and rule shapes
are this repo's business, and several ported rules are better than their chart
originals. A metric no rule here reads is a condition with no witness anywhere.

Waivers live in `scripts/chart_alert_waivers.txt`, keyed on the
`(chart alert, metric)` pair, and **the reason is mandatory - a reasonless line
is a parse error, not a pass.** A waiver says "this condition is watched here,
by a rule keyed on a different metric"; it never says "this condition does not
matter". If a condition is genuinely not worth watching, delete the alert from
the producer's chart rather than waiving it here, or the specification lies.

Three shipped waivers, all of the first kind. The one worth reading is
`TataraSweepStalled`: porting it would have been a **regression**, not a gap
being closed. It is a flat staleness threshold, and this repo deliberately
removed exactly that shape after a flat `21600s` bound was breached for roughly
18 hours of every 24 by the nightly crons. `Operator sweep heartbeat stale`
replaced it with a next-expected timestamp the operator computes per
`(project, activity)` from that activity's own cron - cadence lives in the
producer, one rule covers every Project, and it fires on NoData as well.

Scope, stated so the name does not imply more than it covers: this checks the
**tatara-operator** chart's `PrometheusRule` and nothing else. `tatara-memory`'s
chart also ships one, but the operator provisions it per-Project at runtime and
labels it through `MEMORY_MONITOR_LABELS` - a different mechanism, on a path no
static file read can see. Clone failure is a **hard** failure here rather than
the neutral skip section 5 uses, and that costs no availability:
`check_label_provenance.py` in the same job already clones tatara-operator and
already fails closed on it.

```sh
python3 scripts/check_chart_alert_parity.py    # needs network for the clone
```

## 10. The routing contract: which labels decide delivery

Sections 1-9 all assert that a rule is *correct*. Every one of them assumes it is
*delivered*. Delivery is decided by label strings restated by hand in every rule,
and until issue #117 nothing verified them.

The live notification policy tree (owned by `infra/terraform/grafana`, not this
repo):

```
root                        receiver=Default
`-- homelab="true"          receiver=Default,  mute_time_intervals=["Default"]
    |-- system="tatara"     receiver=Tatara    -> /operator/webhooks/tatara/grafana
    |-- severity="critical" receiver=Critical
    `-- page="true"         receiver=Critical
```

First matching child wins. No route sets `continue`. `Default` is one email
address and mutes all day Saturday and Sunday. `Critical` is an unmuted email
plus a webhook to `/operator/webhooks/`**`infrastructure`**`/grafana` on a 4h
repeat - a different project's endpoint, so reaching it does **not** mint a
tatara incident Task either. The only incident-minting path is `system="tatara"`.

### The contract

| severity | `homelab="true"` | `system="tatara"` | `page` |
|---|---|---|---|
| `critical` | required | required | absent |
| `warning` | required | required | absent |
| `info` | required | **must be absent** | absent |

An unrecognised `severity` is a hard failure, not a pass: the contract is keyed on
severity, so a value with no arm has no defined delivery. A fourth severity needs a
route - and an arm in the checker - before a rule may carry it.

`component` is not part of the predicate and is not asserted.

### What each direction costs

| defect | lands on | consequence |
|---|---|---|
| `warning` omits `system` | the `homelab` node | one email, **muted every weekend**, and it **never mints an incident Task** |
| `critical` omits `system` | `severity="critical"` | the Critical receiver: not muted, but the *infrastructure* webhook - so still **never mints a tatara incident Task** |
| `info` gains `system` | `system="tatara"` | mints an incident Task per fire, for a trend rule |
| any rule carries `page` | `page="true"` | escalates an unrouted rule to Critical; inert and misleading on a routed one |
| any rule omits `homelab` | root | still emails, but loses the mute window and the grouping |

Rows 1 and 2 are the ones with teeth: such a rule passes every other check here,
passes `terraform validate`, plans, applies green, evaluates correctly and fires
correctly, and never reaches the agent platform. Same silent-green shape as
sections 5 and 7, one surface over. Note that the two severities land on
**different nodes** - the weekend mute is a `warning` problem, not a `critical`
one, and telling a critical author otherwise sends them looking for a mute window
that does not apply.

Row 3 is not hypothetical. The two trend rules added for #457 (`Repository phase
desync still being produced`, `Ingest job creation race still being hit`) shipped
carrying `system=tatara` and were corrected by a human reading label sets. **Do not
"harmonise" the label sets across severities.** `severity: info` means "email only",
and that is a deliberate severity gate, not an inconsistency.

Row 4 is why `page` is asserted absent even though no rule uses it: it is the third
live child, it reaches the same unmuted Critical receiver, and it sits *below*
`system=tatara` in first-match order. So it can only bite the rules with no `system`
- precisely the `info` rules the severity gate says must be email-only.

### `labels` is effectively required

`modules/grafana_alert/main.tf:124` is a ternary, not a merge:

```hcl
labels = length(rule.value.labels) > 0 ? rule.value.labels : each.value.default_labels
```

A rule's own labels **REPLACE** the module default. All 130 rules declare their own,
so the false branch was unreachable, and the `homelab = "true"` that used to sit in
`grafana.tf`'s `local.alert_tags` could never be read. That local is deleted: a
safety net that cannot fire is worse than none, because it told the next author that
omitting `homelab` was survivable. The module input stays declared (it is vendored
from `infra/terraform`; keep it byte-aligned), so the fallback now renders *no*
labels - which routes to the root receiver. The checker asserts `labels` is present,
non-empty and a mapping, which is what keeps that branch unreachable.

### Terraform coercion is part of the predicate

`labels` is typed `map(string)`, so an unquoted YAML `homelab: true` is a bool in the
file and the string `"true"` by the time Grafana sees it - the rule routes correctly.
The checker compares values after the same coercion. Quote them anyway for
consistency with the other 130 rules, but a red build on a rule with no routing
defect is how a hard gate gets argued down to a warning (see 6.4).

### The waiver

A rule-level `tatara_routing_justification` (non-empty string) waives the severity-keyed
`system` and `page` arms **for that rule only**:

```yaml
  - name: "An info condition that really must mint a Task"
    tatara_routing_justification: "see #NNN - this trend needs an incident, not an email"
    labels:
      homelab: "true"
      system: "tatara"
      component: "operator"
      severity: "info"
```

It does **not** waive `homelab`, severity validity, or `labels` being a non-empty
mapping: there is no legitimate reason for a tatara alert to leave the homelab
subtree. Waived rules are printed by the checker, so the override is visible in CI
output as well as in the diff.

**A justification that waives nothing is a violation.** A stale key left on a rule
whose labels already satisfy the contract is invisible to review and arms itself
silently the day someone changes those labels - the exception then applies with a
reason written for a condition that no longer exists. Delete it instead.

`tatara_routing_justification` is a LINT-ONLY key (section 7): it is registered in
`check_alert_schema.py`'s `LINT_ONLY_KEYS["rule"]` and relies on the silent drop to
stay out of Grafana. It is rule-scope only - at file scope it is an undeclared key
and fails the schema check.

```sh
python3 scripts/check_routing_labels.py    # offline, no cluster, no token
```

## 11. The series exists: the one question the producer checks cannot ask

Sections 5, and the two reverse-drift checks it names, all validate the metric
**name at its producer**: allowlisted here, emitted by the producer repo's live
Go source, labels declared. Section 6 validates the **comparison**. Section 10
validates **delivery**. None of them can tell you whether Prometheus actually
holds the series.

`tatara-claude-code-wrapper#189` is what that costs. `ccw_commit_push_total` and
`ccw_turns_total` were allowlisted, emitted at the producer's `main`, correctly
labelled, correctly prefixed, and `operator_push_series_dropped_total` was 0 on
every reason - every check in this file green, and the two rules reading them
had never been able to fire. A `prometheus.CounterVec` child does not exist
until its first `Inc()`, so a counter the wrapper only writes at turn end or at
shutdown produced a series with one sample or none, and the wrapper's push
client stopped and DELETEd the run before the shutdown writes even happened.
`default_no_data_state: "OK"` reported both rules green throughout.

The sharper form, and the reason this check queries **selectors** rather than
metric names: the family can be live while the child a rule reads is dark.
Measured at the time of the fix, `ccw_turns_total{result="complete"}` existed
fleet-wide while `ccw_turns_total{result="failed"}` - the numerator of "Wrapper
turns erroring" - had never existed anywhere. `clamp_min(..., 0.0001)` guards the
denominator against an idle fleet; nothing guarded the numerator, and
`empty / 0.0001` is empty. A name-level check would have called that metric live.

`scripts/check_series_liveness.py` asks Prometheus, once per selector, through
the Grafana datasource proxy.

**It never gates a PR, and this is not laxity.** A dark metric is a FLEET
condition, not a property of the change in front of you: a rule can be correct
on the day it merges and go dark three releases later when a producer stops
incrementing. Blocking an unrelated PR on that helps nobody, and a check wired
to `pull_request` would also drag a Grafana credential into every rule lint -
`alert-rules-lint.yml`'s header states it deliberately needs none. It runs on a
schedule from `.github/workflows/series-liveness.yml`; its unit tests DO run in
the lint job, offline, against an injected query function.

**Exit 0 = the check ran, whatever it found. Nonzero = the check could not
run** - no credentials, transport failure, a query Prometheus rejected. The two
must not share an exit code: a liveness assertion that has silently stopped
asserting is the same silent-green failure it exists to catch.

```sh
GRAFANA_URL=... GRAFANA_API_KEY=... python3 scripts/check_series_liveness.py
```
