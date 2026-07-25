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
AND `math_operator` is `<`/`<=` AND a `kube_*` metric appears in the expression.

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
rule as input) is treated as unguarded rather than silently passed.

To keep an unguarded quantile, set a non-empty `tatara_idle_quantile` annotation
saying why that histogram is never idle.
