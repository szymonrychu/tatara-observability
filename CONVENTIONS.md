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

## 5. The CI provenance checks: no alert AND NO PANEL on a metric, label or value nobody emits

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

### 5.1 A selector has three dimensions, and all three are guarded

A PromQL selector says three separate things, each of which can go dark on its
own. Until #100 this repo guarded two of them and had never looked at the third.

| Dimension | Derived from | Enforced by |
|---|---|---|
| metric NAME | the Prometheus constructor's `Name:` field | `check_metric_provenance.py` (alerts/dashboards -> allowlist) + `reconcile_metric_provenance.py` (allowlist -> producer source) |
| label NAME | the SAME constructor's `[]string{...}` slice | `reconcile_metric_provenance.py` |
| label VALUE | the operator CRD's `+kubebuilder:validation:Enum=` markers, plus `internal/stage`'s reason slices for `stateReason` (which has no CRD enum) | `reconcile_metric_provenance.py` |

`operator_task_terminal_total` is the case that motivated it. Five rules selected
`{stage="failed"}` and `{stage="parked"}` on a metric labelled
`{kind,state,stateReason}` since v2.0.0, and both guards reported OK: the metric
name was emitted, `stage` was one of the four label names the old value sweep
knew about, and `failed`/`parked` were both still members of the `## stage`
section of the hand-maintained `stage_values_allowlist.txt`. **A membership test
against a stale SUPERSET is silent by construction**, which is why that file no
longer carries any vocabulary: both label dimensions are re-derived from the
producer clone `reconcile_metric_provenance.py` already takes, on every PR, every
push, and nightly. `scripts/label_exemptions.txt` holds only the deliberate
per-metric exemptions and the infra-label list.

Three rules make the label checks quiet enough to stay switched on:

1. **They apply only to metrics whose constructor was located in a clone AND
   whose label slice resolved.** That one rule removes the entire foreign-exporter
   noise class without a single allowlist entry - no tatara repo declares
   `volume_manager_total_volumes` or `kube_pod_status_ready`, so their labels are
   never these checks' business. A slice built from a variable maps to "unknown",
   reported loudly in the job summary, never a failure. Guessing the empty set
   there would turn every live selector on that metric dark.
2. **Infra labels are exempt by name, once.** `job`, `instance`, `namespace`,
   `pod`, `container`, `service`, `endpoint`, `node` and the `exported_*` collision
   prefix are attached by Prometheus and by the scrape config's relabelling; no
   constructor can ever declare them. Without that list the label-NAME check
   reports ~280 findings on a completely healthy rule set.
3. **Both checks are metric-aware, defaulting to CHECK.** `kind` is the CRD's
   `TaskSpec.Kind` enum on the Task family and an access class, a CR kind or a
   decline class elsewhere; `stage` survived v2.0.0 as a label name carrying three
   different vocabularies. A new metric that overloads a closed-set label, or that
   carries a relabelled label name, fails CI until someone lands it in
   `label_exemptions.txt` on purpose, with the reason. `stage` itself is NOT
   exempted wholesale: it binds to the `TaskStatus.State` enum, because
   `operator_stage_drift_total{stage}` is passed `task.Status.State` verbatim.

4. **The value sweep prints its own coverage.** The label NAME dimension is fully
   derived: whether a metric declares a label is a fact in the source. The label
   VALUE dimension is not, and this is worth being blunt about - a label name's
   vocabulary is a property of the **(metric, label) pair**, and nothing in the CRD
   says which metric's `kind` is `TaskSpec.Kind` and which is a CR kind, a webhook
   event kind or a reaped-resource kind. So `label_exemptions.txt` IS a
   hand-maintained binding, which is the one thing #100 set out to delete. Two
   things make that honest rather than a relapse: it fails **loud** (an unaudited
   overload turns CI red on correct work; it never silently passes a dead value),
   and every run prints the exact (metric, label) pairs in scope, so under-coverage
   is visible in the job summary instead of inferred from two files.

   The first cut of that audit read one repo's `internal/obs` and generalised, and
   review found nine wrong pairs - including `ingest_stage_duration_seconds{stage}`
   in a different repo entirely, which `dashboards/ingester.json` already aggregates.
   If you add a `<label>:exempt-metrics` entry, trace the `WithLabelValues` call site
   first and write what you found next to it.

A **negative** matcher (`!=`, `!~`) on a label the metric does not carry matches
every series, so it is a no-op filter rather than a dark selector - reported as
informational, never fatal, because it is also the forward-compatible idiom for a
matcher written before the producer adds the label. A dead closed-set VALUE in a
negative matcher IS fatal: the rule means to exclude it, so a renamed vocabulary
silently stops excluding and starts firing on exactly what the summary says it
ignores.

### 5.2 What three green dimensions do NOT certify

**A label name and a label value are mechanical. A threshold and a summary are
not.** Nothing in CI catches a rule whose threshold is wrong for the mechanism it
watches, or whose summary describes a mechanism that does not exist - that is
still a human reading the rule, and it stays that way. Do not read a green
`alert-rules-lint` as "this alert is correct"; read it as "this alert selects a
metric that is emitted, on labels that exist, with values that are live".

Run it locally:

```sh
pip install pyyaml
python3 scripts/check_metric_provenance.py            # dimension 1: metric NAMES
python3 scripts/reconcile_metric_provenance.py        # dimensions 2 + 3 (clones 4 repos)
python3 -m unittest discover scripts -p 'test_*.py'    # linter self-tests (both checkers)
```

## 6. Structural alert-shape checks

`scripts/lint_alert_rules.py` enforces three more conventions beyond section 3's
filter-or-justify. All three are deterministic from rule text alone, so they have
no false failures. Each is justify-able with a named annotation, so a deliberate
exception is greppable rather than remembered.

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
