# MEMORY.md - tatara-observability

Past decisions + context. One dated line per entry.

- 2026-07-18: Issue #342 - "Operator sweep heartbeat stale" false-fired on every 4h cycle and every
  operator rollout. Root cause A: `threshold: 7200` (2h) sat BELOW the sweep cadence (issueScan cron
  `0 */4 * * *` = 4h/14400s), so a HEALTHY heartbeat sawtooths 0->~14400s and crossed >7200 for ~2h of
  every 4h period (refires were exactly 4h apart). Root cause B: the gauge is process-local and
  `no_data_state: Alerting`, so every leader rollover blanked it into a NoData page until the next 4h
  sweep. Fix: threshold 7200->18000 (5h, clears the 4h peak by 1h so it fires only on a genuinely
  missed cycle) and `for` 10m->30m (rides over the brief failover NoData/scrape gap). Kept
  `no_data_state: Alerting` deliberately (a truly-absent operator must still PAGE) - the operator side
  (tatara-operator #342) now re-seeds the gauge from `Project.status.lastSweepSuccess` on leader
  startup so a rollover no longer produces benign NoData. Same class as #50/#33 (a raw gauge age /
  metric-absence read paging a healthy fleet).
- 2026-07-12: Issue #50 - "Operator replica missing" warning false-fired on environment cold start /
  Prometheus restart. `count(up{job="tatara-operator"} == 1) or vector(0) < 3` fabricated a literal 0
  when every `up` series was transiently absent, and 0 < 3 held the false page for the whole 15m
  `for:` window while all 3 replicas were healthy - same class as #33 (metric absence != zero
  replicas). Fix (minimal, issue's option 1): dropped `or vector(0)` so an empty `up == 1` set is
  NoData -> OK (default_no_data_state). `count(up == 1)` still excludes down targets (up=0), so a
  genuine 1-2/3 degradation fires; a full 0/3 outage is deliberately NOT this warning's job (NoData ->
  OK here) and stays covered by the CRITICAL "Operator scrape target down" (sum(up) or vector(0) < 1)
  + "Operator pod not ready". Audited every other `math_operator: "<"` rule per the issue's 2nd
  acceptance bullet: the remaining five (`scrape target down`, `deployment has no available replicas`,
  `pod not ready`, `reconcile loop wedged`, `scan loop stalled`) all use threshold 1 = total-outage
  detectors where `or vector(0)` fabricating a 0 on series-absence is the INTENDED behavior (they
  should page when the operator is entirely gone). "Operator replica missing" (threshold 3, a subset
  count) was the only partial-degradation rule carrying the anti-pattern; it appears nowhere else.
- 2026-06-25: Repo created to make tatara Grafana alert rules agent-adjustable. They originally
  shipped in `infra/terraform/grafana` (gitlab.com), which the tatara agents do NOT enroll on, so
  the platform could not tune its own observability. Migrated the alert RULES here (github,
  enrolled on the tatara Project); kept the contact point + notification policy in
  `infra/terraform/grafana` (global homelab routing, label-based). Design:
  `tatara` repo `docs/superpowers/specs/2026-06-25-tatara-observability-migration-design.md`.
- 2026-06-25: Owns its own Grafana folder `Tatara` (infra/terraform keeps `Default`) so the two
  terraform states never collide - disjoint folders + disjoint rule-group names.
- 2026-06-25: `modules/grafana_alert` vendored from infra/terraform, including the `loki`
  query_type support (loki model + `expr`) added for the Loki log-burst / internal_issue_report
  rules. Keep it in sync if the infra module changes.
- 2026-06-25: Routing depends on every routed rule carrying BOTH `homelab=true` (matches the
  homelab parent policy) AND `system=tatara` (matches the child route to the operator incident
  webhook). Per-rule `labels` REPLACE the module `default_labels` (no merge). `info` rules omit
  `system` so they email only (severity gate).
- 2026-06-28: Added `alerts/tatara-cd.yaml` (semver push-CD, design
  `docs/superpowers/specs/2026-06-28-semver-push-cd-design.md` sec 8.4) - two critical rules over the
  operator G5 leader-only gauges `tatara_cd_cascade_failed` and `tatara_cd_cascade_stalled` (>0). Both
  `max(...) or vector(0)` (leader-only -> max picks the leader; `or vector(0)` keeps the rule defined
  when the operator is gone, deferring the outage page to the operator-down rules). `component=cd`,
  `system=tatara`+critical so the existing routing escalates to the operator incident webhook,
  mirroring the internal-issue path. Metrics lack a `_total` suffix = gauges, threshold `>0`. NO scrape
  allowlist widening needed: the operator ServiceMonitor (`tatara-operator/charts/.../templates/servicemonitor.yaml`)
  scrapes the full `/metrics` with NO `__name__` keep filter, so `tatara_cd_*` is picked up
  automatically once emitted (the `pushMetricsAllowedPrefixes` allowlist is the wrapper/ingester
  push-gateway, unrelated to the operator's own scrape).
- 2026-06-25: `alerts/tatara-ingester.yaml` rule "Tatara ingest job failing" MUST keep the
  `mode="full"` selector (`operator_ingest_job_total{...,result="failure",mode="full"}[1h] > 0`).
  The operator attributes ingest failures by `mode` so alerting pages ONLY on terminal full-ingest
  failures; a failed `mode="incremental"` ingest self-heals via the full-ingest fallback
  (`incrementalFallbackThreshold`) and is benign. Window/threshold mirror the operator's canonical
  PrometheusRule `TataraIngestJobFailing`. Do NOT drop `mode="full"` (issue #3 - it paged on
  benign incremental churn).
- 2026-06-25: CI = GitHub Actions terraform, S3 state (`szymonrychu-terraform-state` key
  `terraform/tatara-observability`), Grafana Editor SA token. Secrets are GitHub Actions secrets
  (not sops): `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `TF_VAR_GRAFANA_API_KEY`,
  `TF_VAR_GRAFANA_URL`.
- 2026-06-27: Added `dashboards/task-delivery.json` + `dashboards.tf` - Grafana dashboard "Tatara - Task Delivery" (uid=tatara-task-delivery, schemaVersion=39). 5 panels: open-issues table (tatara_issue_state, state+incident color maps, GitHub+GitLab data links), per-issue turns+tokens table (joinByLabels on tokens+turns sum by project/repo/issue/kind; requires Grafana 10.3+ experimental joinByLabels transform; column rename handles Grafana naming variants), inflight-by-kind stacked timeseries, token burn rate, turn rate. Panels 4+5 use new operator_task_turns_total and tatara_issue_state metrics - populate after operator A1/A2 deploy. `dashboards/**` added to workflow path filter. Datasource uid=prometheus (homelab fixed uid).
- 2026-06-27: Removed `alerts/tatara-ingester.yaml` rule "Tatara ingest job kube-state failures"
  (`sum(kube_job_status_failed{...job_name=~".*-ingest-.*"}) > 0`, for 15m). It was a regression of
  the issue #3 lesson above: `kube_job_status_failed` counts EVERY failed pod with no mode
  discrimination and a failed Job lingers ~10m (`TTLSecondsAfterFinished`), so it fired ~permanently
  on benign self-healing incremental churn (BackoffLimit=0) and transient full-ingest retries
  (BackoffLimit=2). It cannot be made mode-aware: kube-state-metrics does not export the
  `tatara.dev/ingest-mode` Job label (no `--metric-labels-allowlist`), so there is no PromQL path to
  filter down to terminal full-ingest failures. Terminal full-ingest failures stay covered by the
  mode-aware operator-native rule "Tatara ingest job failing"; genuine infra failure modes stay
  covered by "Tatara ingest pod OOMKilled", "Tatara ingest pod stuck waiting", and "Tatara ingest
  job stuck active". Issue #10.
- 2026-06-28: Hardened the `tatara-operator` down-detection rules against the scaled-to-zero /
  target-absent case (issue #7). At 0 replicas the series the liveness rules depended on
  (`up{job="tatara-operator"}`, `kube_pod_status_ready`, `operator_reconcile_total`) disappear, the
  aggregations return an EMPTY vector (not 0), and with `default_no_data_state: "OK"` that NoData was
  treated as healthy - so a full ~1h45m operator outage only fired the slow `Operator scan loop
  stalled` warning (the only rule already wrapped in `or vector(0)`). Fix: wrap the up/readiness/
  reconcile expressions in `or vector(0)` (empty -> 0 -> crosses `< threshold`), matching the
  scan-loop pattern, and added a new CRITICAL rule "Operator deployment unavailable (scaled to zero)"
  on `max(kube_deployment_status_replicas_available{deployment="tatara-operator"}) or vector(0) < 1`
  for 5m - kube-state-metrics keeps exposing that series (reading 0) when every `up` target vanishes,
  so it is the robust primary down-detector. Kept `default_no_data_state: "OK"` unchanged on purpose:
  the module supports a per-rule `no_data_state` override (coalesce in modules/grafana_alert/main.tf),
  but flipping the file default to alerting would turn genuinely-absent OPTIONAL series elsewhere into
  noise; `or vector(0)` is surgical, self-documenting, and per-rule. "Operator replica missing" stays
  `warning` (partial 1-2/3 HA degradation); total-outage CRITICAL is covered by the hardened
  `sum(up) < 1` plus the new kube-state-metrics rule.
- 2026-06-28: Fixed `alerts/tatara-wrapper.yaml` rule "Wrapper HTTP 5xx responses" false-firing on
  `/readyz` readiness-probe 503s during agent-pod bootstrap (issue #8; #9 is a duplicate). The
  wrapper mounts `httpMetrics()` router-wide, so each kubelet probe 503 increments
  `ccw_http_requests_total{route="/readyz",status_code="503"}` while `ctl.Alive()` is false (clone +
  hook install + Claude start). With `>0 for 10m` on a rate, a ~10m slow bootstrap tripped the page
  even though the real API emitted zero 5xx (`/v1/*` turn-submit returns 202/409; the operator
  turn-complete callback runs on the loopback `InternalRouter()` which carries NO metrics middleware,
  so it cannot emit `ccw_http_requests_total` at all). Fix: exclude probe routes from the 5xx
  selector - `route!~"/readyz|/healthz|/metrics"`. Kept `>0 for 10m`: once probe 503s are excluded a
  sustained real-API 5xx is genuinely abnormal, and readiness problems stay covered by the dedicated
  "Wrapper agent pods not becoming ready" rule. Also swept the now-stale
  "(DARK until pushMetricsAllowedPrefixes widens.)" caveat from ALL `ccw_*` rule annotations and the
  INERT/DARK header + section comments: those families went LIVE when the operator push allowlist was
  widened (operator 12729ed). Pairs with the app-side `/readyz`-503 tolerance fix in tatara-chat #45.
- 2026-06-28: Pinned `terraform {plan,apply} -parallelism=1` in `.github/workflows/apply.yml`. The
  post-merge `terraform apply` of the #8 wrapper fix went red even though the change itself was
  correct and DID land in Grafana: `PUT .../rule-groups/tatara-operator` returned `409` and a
  concurrent `GET .../alert-rules` (tatara-wrapper) hit `context deadline exceeded`. Root cause is a
  concurrency race, NOT content: terraform's default parallelism (10) writes/refreshes every
  `grafana_rule_group` into the SAME Grafana folder (Tatara, uid bfq61vizzm4n4f) at once, and the
  Grafana provisioning API 409-conflicts / times out under concurrent same-folder writes (the large
  slow `tatara-operator` rename from #7/#13 widened the window). Proof it was a race: the operator
  group still converged to the desired 22-rule state despite the 409 (a real content conflict would
  have stayed stale). `-parallelism=1` serializes the Grafana API calls so each group applies on its
  own - the simplest root-cause fix, no provider-arg/timeout guessing. Applies are slower but this
  config is tiny (1 folder + 1 dashboard + 6 rule groups). If a single huge group ever times out on
  its own even when serialized, revisit with a provider-level `retries`/`retry_status_codes` bump.
- 2026-06-28: `-parallelism=1` was NOT enough - the next post-merge apply (#15, SHA a7087d7) still
  went red with `Get .../provisioning/alert-rules/<uid>: context deadline exceeded`. The real
  trigger was a PERPETUAL DIFF, not concurrency: `modules/grafana_alert/main.tf` rendered the loki
  model with `queryType="instant"` INSIDE the data-block model JSON but never set the data block's
  own top-level `query_type` attribute, so Grafana persisted `queryType="instant"` on the loki
  AlertQuery while terraform planned it back to null every run (`query_type: "instant" -> null` on
  the 4 `tatara-logs` loki rules => "1 to change" on EVERY apply). That forced a needless rule-group
  write each merge, and the write (plus the genuinely slow Grafana provisioning API - the
  tatara-memory refresh alone took ~86s) tipped over the context deadline. Fix: set the data block's
  `query_type` from the SAME model map - `lookup(local.type_to_model[query_type], "queryType", null)`
  - so loki blocks get "instant" (matches Grafana) and prometheus/math stay unset (Grafana stores
  "", no diff). Verified against live Grafana: tatara-logs loki query (refId A) queryType="instant";
  all prometheus groups' refId A queryType="". With the drift gone the plan is empty, so steady-state
  applies do refresh + 0 changes and never force a write - the durable fix the `-parallelism=1`
  band-aid was masking. The model's `queryType` and the block's `query_type` now share one source.
- 2026-06-28: Killed the recurring false-positive alert class (issue #19, set A1+B1+C1+D2+E1). Added
  `CONVENTIONS.md` (the one normative "real error vs benign/transient" spec, co-located with
  `alerts/`), a deterministic "filter-or-justify" lint `scripts/lint_alert_rules.py` (+ unittest
  `scripts/test_lint_alert_rules.py`) wired as a STANDALONE CI job `.github/workflows/alert-rules-lint.yml`
  (no AWS/Grafana secrets; complementary to #18's dark-rule check, not folded in - E1). The lint flags
  any rule selecting a 5xx status on an `*http_requests_total` family unless the selector excludes probe
  routes OR the rule carries a non-empty `tatara_probe_exclusion` annotation. It immediately caught chat
  (`tatara-chat.yaml` 5xx ratio) and memory (`tatara-memory.yaml` 5xx ratio); both now carry the
  justify annotation. chat's documents its real producer-side exclusion (chat router.go:30-34,51-68);
  memory's documents a KNOWN GAP (latent #8/#9 reintro: mem-* metrics.Middleware is mounted before
  /healthz,/readyz and the rules are currently dark) - the producer-side memory fix is a follow-up
  (D2), see ROADMAP.md. Lint runs locally via `pip install pyyaml && python3 scripts/lint_alert_rules.py`.
- 2026-06-28: Chose Python+PyYAML for the lint (not Go - this repo is "not a code component"; not raw
  text scanning - that is the fragile tech-debt the convention warns against). yamldecode-equivalent
  structural parse; `annotations` is `optional(map(string),{})` in the module so the arbitrary
  `tatara_probe_exclusion` key passes straight through to Grafana.
- 2026-06-28: The #19 post-merge `terraform apply` (SHA fc8c323) still went red with
  `Get .../provisioning/alert-rules/<uid>: context deadline exceeded` - same class as #15, but with
  the perpetual diff already gone (so a genuine 2-annotation change, not drift). It is NOT content:
  the timed-out GET was on the unrelated `tatara-wrapper` group, and a different rule group times out
  on each merge - i.e. a TRANSIENT homelab stall. Ran down the provider internals to settle the next
  step the prior note floated ("revisit with a provider-level `retries` bump"): that bump CANNOT work
  here. The grafana provider's read uses the go-openapi runtime, which pins a fixed 30s per-request
  deadline (`client/runtime.go` `DefaultTimeout = 30s`, `req.SetTimeout(DefaultTimeout)`); it is not
  exposed by any provider argument (schema has only `retries`/`retry_wait`/`retry_status_codes`, none
  a timeout). And the provider's retry transport (`grafana-openapi-client-go` `pkg/transport`) retries
  on any error INCLUDING the deadline, but reuses the SAME already-expired request context, so every
  retry inside one read fails instantly - useless for a >30s stall. The only lever that recovers a
  transient stall is a fresh terraform process (fresh per-request deadlines). Fix: wrap
  `terraform {plan,apply}` in a 3-attempt retry loop in `.github/workflows/apply.yml` (15s/30s
  backoff); terraform is idempotent and converges, so a later attempt lands the change once the API
  un-stalls. Kept `-parallelism=1`. No `grafana_rule_group` `timeouts {}` block exists to raise the
  read deadline (checked the resource schema). Also dropped the two `scripts/__pycache__/*.pyc` files
  the #19 merge accidentally committed and added `__pycache__/` + `*.pyc` to `.gitignore`.
- 2026-06-28: Closed the D2 KNOWN GAP from the 2026-06-28 #19 entry above (issue #21). Took the
  consumer-side half of filter-or-justify on "Memory HTTP 5xx error ratio high": added
  `route!~"/readyz|/healthz|/metrics"` to BOTH the numerator and denominator selectors and removed the
  `tatara_probe_exclusion` annotation (lint now passes on the filter, not the justification). Verified
  the label against source before editing: tatara-memory `http_requests_total` is a CounterVec keyed
  `{route, method, status}` where `route` is the chi route pattern via `routeLabel` and `status` is
  `http.StatusText` (`tatara-memory/internal/httpapi/middleware.go`), and the probe routes are the
  literal chi patterns `/healthz`, `/readyz`, `/metrics` (`router.go`) - so the exclusion is exact, not
  a guess (this is why it had to be confirmed: a wrong label name would make the filter a silent no-op).
  Did NOT do the producer-side fix (mount probes outside `metrics.Middleware`, `router.go:41,43-44`):
  that lives in tatara-memory, out of this repo's blast radius; it is now an OPTIONAL cleanup, no longer
  a scrape prerequisite. The cross-repo CONVENTIONS.md pointers (tatara-documentation observability.md,
  tatara-agent-skills review checklist) also stay as separate-repo follow-ups - neither repo is in scope
  for a tatara-observability PR.
- 2026-07-04: Added `alerts/tatara-quality.yaml` (Task 6, quality-feedback-loop plan, G5): the
  tier-quality rubber-stamp rule reading `operator_review_outcome_total` (Task 1, G4, operator repo),
  find-rate `changes_requested / all` for `model="claude-sonnet-5"` over `[6h]`, gated with
  `and on() (... * 21600 > 5)` so it cannot fire on 1-2 reviews (min-volume >5 reviews/6h). Threshold
  0.02 and the 6h window/for are PROVISIONAL - no G4 baseline data exists yet, see ROADMAP.md.
  `project="tatara"` is the one live Project CR name (`tatara-helmfile/values/project-tatara/common.yaml`
  `name: tatara`), not a template placeholder - single-tenant homelab, so one rule file, not one per
  project. Routing is unchanged from every other rule here: `homelab=true` + `system=tatara` match the
  existing label-based parent/child notification policy in `infra/terraform/grafana` - there is no
  separate literal "project webhook contact point" resource in this repo despite the plan's wording;
  the label pair IS the routing mechanism. Added `scripts/check_tier_quality_alert.sh` (same
  manual-run structural-guard pattern as `check_quality_panels.sh`/`check_token_panels.sh`, not wired
  into CI) as the TDD covering test: asserts the rule exists with the required label set and PromQL
  shape (metric name, verdict/model selectors, `and on()` volume gate). `scripts/lint_alert_rules.py`
  and its `RealAlertFilesPass` unittest ignore this rule (it selects no `*http_requests_total` 5xx
  status - out of that lint's scope), and `terraform validate`/`fmt -check -recursive` pass unchanged
  since `grafana.tf`'s `fileset(alerts/*.yaml)` picks the new file up with no `.tf` edits needed.
- 2026-07-05: Consolidated the two overlapping memory dashboards (`dashboards/memory-server.json`
  uid `tatara-memory-server` 25 panels, and `dashboards/tatara-memory.json` uid `tatara-memory` 27
  panels - both created by a monitoring-audit workflow) into one canonical
  `dashboards/memory.json` (uid `tatara-memory`, 32 panels, `job="tatara-memory"` +
  `service=~"$service"` templating variable). Verified live against Prometheus via the grafana MCP
  (`list_prometheus_metric_names` + `query_prometheus`, not just source-reading): every app metric
  family referenced (`lightrag_calls_total`, `code_graph_query_total`/`_entities_upserted_total`/
  `_edges_upserted_total`/`_analytics_*`/`_compute_duration_seconds`/`_betweenness_skipped_total`,
  `ingest_items_total`/`_jobs_total`/`_item_duration_seconds`/`_notify_dropped_total`/
  `_source_index_errors_total`/`_store_op_errors_total`, `tatara_memory_op_total`/
  `_tombstone_total`, `http_requests_total`/`_request_duration_seconds`, and the operator-side
  `operator_memory_provision_duration_seconds`/`operator_lightrag_query_errors_total`/
  `operator_memory_retrieval_probe_total`/`operator_tool_surface_probe_total`) is live-scraped, with
  real `service` label values `mem-tatara`/`mem-infrastructure` confirmed on `job="tatara-memory"`
  targets. This CLOSES the ROADMAP "planned: wire mem-* pods to scrape" item (Gap 1) - it was stale;
  the pods have been scraped for a while, `alerts/tatara-memory.yaml`'s own header comment already
  said so. No panel needed a "dark, not-yet-scraped" disclaimer as a result - every metric family in
  the merged dashboard is real and live. Dedup calls made: kept the `job=tatara-memory` filter
  convention (avoids the documented `http_requests_total` name collision with tatara-chat) AND added
  back memory-server.json's `$service` template variable (per-project `mem-tatara`/
  `mem-infrastructure` breakout) since it is a real, live label - confirmed via MCP query, not
  assumed. Kept memory-server.json's absolute-rate "Code-graph query errors by op" panel over
  tatara-memory.json's ratio version because it mirrors the actual deployed alert's PromQL shape
  (`alerts/tatara-memory.yaml` "Memory code-graph query errors" fires on absolute rate>0, not a
  ratio). Kept the unique "Ingest silent-loss errors" panel (memory-server.json) and the unique
  "HTTP API" row + refined "Memory op error ratio" / "Analytics compute latency p95/p99" panels
  (tatara-memory.json). All rate/histogram windows normalized to a fixed `[5m]` (`[30m]` for the
  slow-moving analytics-backlog panels) rather than `$__rate_interval`, since the latter can resolve
  under 5m on this dashboard's time range and the task required rate windows >= 5m explicitly.
  SEPARATE FINDING (not fixed, flagged in ROADMAP): `dashboards.tf` only has 3
  `grafana_dashboard` resources (task_delivery, quality_feedback, claude_usage_windows) - neither
  memory dashboard, nor `chat*`/`wrapper*`/`ingester`/`operator*`/`agent-lifecycle` dashboard JSON
  files are wired to any Grafana dashboard resource at all. RESOLVED IN THIS SAME PR: the orchestrator
  ran the same consolidation for chat/wrapper/operator (each had 2-3 duplicate boards), deleted all 9
  redundant JSONs, and wired every canonical dashboard (operator/wrapper/memory/chat/ingester/
  agent-lifecycle) into `dashboards.tf` - so `memory.json` and the others ARE now applied. `terraform
  validate`/`fmt` + `scripts/lint_alert_rules.py` green.
- 2026-07-04: Added `dashboards/claude-usage-windows.json` (uid=tatara-claude-usage-windows) +
  `alerts/tatara-usage-gate.yaml` for the claude-subscription-usage-gate feature (Phase C of
  `tatara` docs `docs/superpowers/plans/2026-07-04-claude-subscription-usage-gate.md`). Dashboard:
  per-window utilization (timeseries + gauge) from `tatara_account_usage_utilization{window}`, reset
  countdown from `tatara_account_usage_resets_at_seconds - time()`, per-kind admission holds from
  `operator_admission_blocked_total{reason="kind_ceiling"}`, read-only monthly overage from
  `tatara_account_overage_{percent,used,limit}` - all confirmed verbatim against tatara-operator
  `internal/obs/operator_metrics.go` (worktree `feat/usage-window-gating`). Two panels + one alert
  rule are PENDING OTEL DEPLOYMENT and explicitly labeled as such: `claude_code_cost_usage` (real
  cost) and `claude_code_api_error{status_code="429"}` (reactive backstop) - these are the
  Prometheus-normalized names the operator's own plan/progress notes commit to
  (`.superpowers/sdd/progress.md:26` in the operator repo), translated from the documented dotted
  OTel instrument names (`claude_code.cost.usage`, `claude_code.api_error`); the exact suffix the
  OTLP->Prometheus collector's exporter adds (unit/type) is NOT yet confirmed since that collector
  ships later (Phase D, tatara-helmfile) - fix the query if the deployed name differs. Emergency-
  ceiling alert threshold (80%) reuses `budget.DefaultEmergencyPercent` (tatara-operator
  `internal/budget/budget.go:38`) as the closest existing platform "emergency" convention, flagged
  PROVISIONAL pending real five_hour/seven_day utilization data, matching the tier-quality rule's
  precedent above. `terraform fmt`/`validate` pass (ran `init -backend=false` locally, no state
  touched); `lint_alert_rules.py` + its unittest pass unchanged (the 429 rule selects no
  `*http_requests_total` family, out of that lint's scope).
- 2026-07-05: Fixed the "Agent token spend runaway series missing (coverage gap)" rule in
  `alerts/tatara-operator.yaml` (issue #36). It used a raw
  `absent(operator_task_tokens_total{model=~".+", type=~"..."}) > 0`, which pages on ordinary
  idleness: `operator_task_tokens_total` is an ephemeral per-task counter (series created on the
  first token-bearing turn, deleted on task GC by the reaper - tatara-operator
  `internal/obs/operator_metrics.go` AddTaskTokens/DeleteTaskSeries + `controller/reaper.go`), so
  the selector is legitimately empty during any idle/cold-start window >=15m. `absent()` could not
  tell the benign idle case from the real regression the rule guards (operator emitting token
  series WITHOUT the model/cache labels - the pre-#220 state). Gated the check on token activity:
  `(count(operator_task_tokens_total) > 0) and absent(...{model=~".+", type=~"..."})`, so it only
  fires when tokens ARE being emitted but none carry the model/cache labels, and stays silent
  (NoData -> OK, same as the healthy-present case) when idle. Also refreshed the stale summary +
  comment block (the model/cache schema shipped in tatara-operator #220; old text said "deploy the
  metrics before relying on it"). The `and`-gated-threshold pattern is idiomatic here (see the
  turn-submit/restapi error-rate rules). `lint_alert_rules.py` passes.
- 2026-07-05: Issue #33 - the CRITICAL "Operator deployment unavailable (scaled to zero)" rule
  (added 2026-06-25, above) false-paged from a transient kube-state-metrics scrape gap: its
  `or vector(0)` fabricated a 0 whenever `kube_deployment_status_replicas_available` went briefly
  absent. Human @szymonrychu: "If something is scaled to 0, this alert shouldn't fire. Scaling to 0
  is intentional." Reworked into "Operator deployment has no available replicas":
  `max(kube_deployment_status_replicas_available{...}) and (max(kube_deployment_spec_replicas{...})
  >= 1) < 1` for 5m. Two changes: (1) dropped `or vector(0)` so a KSM absence is NoData -> OK
  (default_no_data_state) instead of a fabricated 0; (2) gated on spec_replicas>=1 so an intentional
  scale-to-zero (spec=0 -> guard drops the series -> empty -> NoData -> OK) never pages. Only fires
  when the Deployment is scheduled to run but has zero available replicas (unschedulable / all pods
  failing readiness). Contradicts the 2026-06-25 reasoning that this rule was the "robust primary
  down-detector" via `or vector(0)`: full-outage/Deployment-deleted coverage instead stays on
  "Operator scrape target down" (`sum(up) or vector(0)`) + "Operator pod not ready", both
  KSM-independent. Verified the `and`-gate against live Prometheus (spec=available=3 -> value 3;
  false guard -> empty). `lint_alert_rules.py` passes.
- 2026-07-05: The #33 merge's post-merge `terraform apply` (SHA bafb4f4) went red - NOT content:
  all 3 retry attempts hit `context deadline exceeded` on a GET during the state refresh, each on a
  DIFFERENT resource (alert-rules cfr6fz1u0r9c1e/afr3zl2erjtoge, dashboard tatara-chat, alert-rules
  cfq61vwizchs0f) - the same transient homelab provisioning-API stall class documented across the
  2026-06-28 entries. The existing mitigation (`-parallelism=1` + 3x whole-process retry) was not
  enough because the API stayed slow for the entire ~11m window, and EVERY attempt re-refreshed all
  ~34 rule reads, so each one had a fresh chance to stall on some random resource. Durable fix in
  `.github/workflows/apply.yml`: keep attempt 1 as a full-refresh apply (still reconciles genuine
  out-of-band drift on the happy path), but drop retries to `terraform apply -refresh=false`. State
  is authoritative here (this repo is the sole, bot-only writer of the Tatara folder), so -refresh=false
  plans config-vs-state and touches ONLY the actually-changed rule group - cutting the provisioning-API
  read surface from ~34 GETs to ~1 (the changed group's post-write read-back), so a fresh process rides
  out the stall and lands the change instead of re-stalling on an unrelated refresh GET. This attacks
  the root cause the 2026-06-28 retry-loop band-aid was masking (the mass refresh, not the retry count).
  PR plans are untouched (still refresh -> drift stays visible in the sticky plan comment). Chose this
  over -refresh=false-on-all-attempts (attempt 1 keeps drift reconciliation when the API is healthy)
  and over just bumping the retry count (probabilistic; would not have ridden out an 11m slow window).
- 2026-07-06: Added "Memory postgres or neo4j container stuck waiting" to `alerts/tatara-memory.yaml`,
  closing the gap that let both `mem-tatara-pg` cnpg REPLICAS crashloop ~8h at 1/3 ready (HA=0) with
  ZERO alerts. Root: every pod-lifecycle rule here scopes to the memory API pod and EXPLICITLY excludes
  the backing stores (`pod!~"mem-.*-(neo4j|pg|lightrag).*"`), and "Memory stack stuck not ready"
  (`operator_memory_stacks` phase=Provisioning|Failed) stayed silent because the memory API kept SERVING
  via the surviving primary - the stack read Ready while HA was gone. New rule mirrors "Memory API
  server container stuck waiting" for the stateful members:
  `kube_pod_container_status_waiting_reason{pod=~"mem-.*-(pg|neo4j)-[0-9]+",reason=~"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerError|CreateContainerConfigError"} > 0`
  for 10m, critical. Chose the container-waiting-REASON signal over pod-not-ready deliberately (review
  finding): a legit replica re-clone stays Running-but-not-Ready through basebackup/catchup (can exceed
  10m on a large DB) and would false-fire a not-ready rule, but is NOT in a waiting reason, so the
  reason-keyed rule has no re-clone noise while still catching the crash loop + the CephFS
  CreateContainerError class. Anchored `-(pg|neo4j)-[0-9]+` (PromQL matchers are fully anchored) matches
  only stable instance pods, not transient `-join-`/basebackup clone pods; lightrag omitted (Deployment,
  hashed pod names; its outages surface via the LightRAG/API rules). Critical not warning: a member
  stuck 10m is one failure from the full-outage circular deadlock, and it is still-serving so the
  incident agent can act. Verified: lint + self-tests pass; a range query over the 2026-07-06 incident
  window returns exactly `mem-tatara-pg-1`/`pg-3` reason=CrashLoopBackOff=1 for ~8h and nothing else, so
  the rule provably fires on the real incident with no false positives. Root cause (operator-default 2Gi
  WAL volume too small) fixed in tatara-operator#270 (default 2Gi->8Gi) + tatara-helmfile#140.
- 2026-07-12: **The dead-alert class, and the guardrail that ends it.** The task-centric redesign
  deletes `Task.status.phase` / `lifecycleState` / `cascadeStage` / `implementGiveUps` /
  `linksSyncFailures`, plus `changeSummary`+`PROutcome`, `writebackSkip4xxAttempts`, `disarmFailures`
  and the whole issueLifecycle machine (contract A.5, Global Constraint 9). Every alert file sets
  `default_no_data_state: "OK"` and `grafana.tf:28` sets `default_exec_err_state = "OK"`, so a rule
  whose metric vanishes does NOT fire and does NOT go stale - it reports OK forever. Counted this
  repo's diff directly (both CD-cascade rules, `Operator scan loop stalled`, the four
  `operator_writeback_outcome_total{result=...}` rules (404-loop + both disarm rules + sibling-links-
  capped), `Operator tasks inflight pinned at cap`, `Operator task lost pod terminations`,
  `Operator lifecycle give-up spike`, and two rules rewritten in place because a LABEL went dead while
  the metric name survived - `Operator task failure spike` (`phase="Failed"`) and `Wrapper metrics
  blind while agents running` (`operator_tasks_inflight`)): **12 rules**, not the contract's 8 nor the
  plan draft's arithmetic of 13 (the plan's own census bullet list double-counts
  `Operator sibling-links sync permanently capped` as both "one of the contract's 8" AND one of "five
  more" - correct arithmetic is 8 + 4 new = 12). Two of the twelve were the CD-cascade rules, i.e. the
  merge/deploy path to a cluster-admin-scoped runner would have had zero alert coverage while every
  dashboard read green. Fix: `scripts/check_metric_provenance.py` + `scripts/metrics_allowlist.txt`
  make "alert on a metric nobody emits" a CI failure, and (v7 addition) also validate
  `stageReason=`/`stage=`/`kind=`/`agent_kind=` label VALUES against `scripts/stage_values_allowlist.txt`,
  since a live metric filtered on a dead label value reports OK forever the same way. TRADEOFF
  ACCEPTED: the allowlist is hand-maintained and must be updated in the same PR as the operator change
  that adds or retires a metric. That coupling is the feature - it forces producer and consumer to
  move together.
- 2026-07-12: **`no_data_state: Alerting` on heartbeats only.** Exactly one rule
  (`Operator sweep heartbeat stale`) overrides the file default, and it overrides `exec_err_state`
  too. For a heartbeat, NoData IS the failure and a Prometheus outage reporting OK is the same failure
  wearing a hat. For every other rule - gauges that legitimately disappear when the fleet is idle -
  `OK` remains correct, and setting `Alerting` on them would page every quiet night. Do not
  generalise this to any other rule.
- 2026-07-12: **Concurrency gates pods, not Tasks.** `maxConcurrentTasks` became
  `maxConcurrentAgents` and the admission unit is the pod-spawn. Tasks are now long-lived scaffolding:
  one sitting in `clarifying` for 24h is healthy. Every alert that counted Tasks as a proxy for "work
  in flight" now counts `kube_pod_container_status_running{namespace="tatara",container="wrapper"}`
  instead. THRESHOLD COUPLING: the agent-pod saturation threshold hardcodes `5.999` = `2 x
  maxConcurrentAgents`, and `tatara-helmfile` sets `maxConcurrentAgents: 3` on both Projects (fleet
  ceiling 6 pods). Bumping `maxConcurrentAgents` in helmfile REQUIRES bumping this threshold in the
  SAME change, or the saturation rule silently stops meaning what its summary says. Tracked as an open
  item in ROADMAP.md too, because it is a cross-repo coupling nothing enforces mechanically.
- 2026-07-12: **CROSS-REPO DEPENDENCY D1 is STILL OPEN, not verified.** Contract K.1 does not name
  `operator_task_terminal_total`, but without a terminal-transition COUNTER there is no failure-rate
  alert on the new surface (`operator_task_stage` is a gauge and a `failed` Task lingers 7 days, so a
  level rule latches for a week after one bad hour). The design requires it to survive with `phase`
  swapped for `stage` + `stageReason` + `kind`. As of this PR the operator worktree
  (`.worktrees/feat/task-centric-redesign/tatara-operator` @ c5c1c40, per `scripts/metrics_allowlist.txt`'s
  own header) has NOT been rewritten: `internal/obs/task_metrics.go:31-33` still declares
  `{kind,phase,reason}`. Several alerts in `tatara-cd.yaml`/`tatara-operator.yaml` and both dashboards
  are therefore written on an UNVERIFIED assumption about the merged operator's label set. Re-verify
  against the actual merged operator branch BEFORE the release train ships (Task 2 step 3's grep, one
  more time); if the operator PR drops the metric or ships a different label name, the Task 6
  fallback applies and every rule built on it changes in the SAME PR. Recorded as an open item in
  ROADMAP.md.
- 2026-07-12: **One terraform edit, granted explicitly:** `dashboards.tf:37-41` (the chat dashboard
  resource) had to go with `dashboards/chat.json`, or `terraform plan` fails on a `file()` against a
  missing path. The "agents never edit terraform" rule stands for everything else; this carve-out is
  now spent and does not reopen for any future change.
- 2026-07-12: **`operator_task_stage_age_seconds` conflates the admission and work clocks, and two
  alerts' thresholds were wrong for that (v6/v7 patch).** The 24h admission-starved clock is armed on
  EVERY POD stage, not just `approved` (fix V6-1); pod-less stages do not run it at all (fix V7-8).
  Stage AGE counts from `stageEnteredAt` regardless of which of the three F.4 clocks is armed, so it
  cannot tell "queued 10h, healthy" from "working 10h, wedged" - and no metric exposes which clock is
  armed. Rather than alert on a metric nobody emits, thresholds are set past each stage's worst-case
  HEALTHY envelope: `Operator pod stage wedged` 9h -> 36h (24h admission + 3 x 5m readiness respawns +
  <=6h work); the old shared `clarifying|approved` rule SPLIT in two because `clarifying` is a pod
  stage (24h admission + 24h work ~= 48h healthy, so 60h) while `approved` is pod-less (24h work only,
  so 36h stands) - a single shared 36h threshold false-fired on any clarify Task that queued 20h then
  worked 20h. The readiness clock itself is 5m (`podReadyTimeout == agentBootDeadline`,
  `task_controller.go:35`), not the 15m an earlier contract draft assumed, and a breach RESPAWNS the
  pod rather than terminating the Task - `pod-not-ready` is explicitly NOT a stageReason (fix V7-7):
  it never made it into `scripts/stage_values_allowlist.txt`, and this is the dead-alert class one
  level below what the metric-name checker can see - a rule filtering on a label VALUE nothing emits
  passes a name-only provenance check and reports OK forever. Pod-less thresholds in `tatara-cd.yaml`
  (merging 4h, deploying 2h) are NOT harmonised upward with the raised pod-stage threshold: pod-less
  stages never carry the 24h admission clock, so a `merging` Task past 4h is a real signal, never
  queue noise.
- 2026-07-12: **v6 patch: three metrics this plan's own census missed on the first pass.**
  `operator_doc_task_abandoned_total`, `operator_unexpected_merge_total` and
  `operator_queue_age_seconds` are all named in contract K.1 but were absent from this plan's original
  allowlist and rule set - the exact defect class K.1 warns about twice, this time almost committed by
  the plan meant to prevent it. Fixed: allowlist entries plus new/rewritten rules
  (`Operator unexpected merge detected`, `Operator documentation batch abandoned`, and the
  incident-starvation rule rewritten off a query - `stage="triaging"` - that per contract fix M20 could
  never fire, since triaging's own budget is 5m).
- 2026-07-12: **`Tatara merge or deploy cycle exhausted` does not use the query K.2 literally
  suggests.** K.2's own text keys it on `operator_task_parked_total{stageReason=~
  "merge-blocked|deploy-blocked"}`, but contract F.3 defines both reasons as FAILED terminals
  (cycle-cap exhaustion, fix H7), never parked ones - that query would never fire. Implemented against
  `operator_task_terminal_total` instead. Internal inconsistency in the contract's own K.2 vs F.3, not
  an observability-side defect; flagged upstream.
- 2026-07-12: **Cross-plan mismatch on `operator_queue_age_seconds`, RAISED AND CLOSED.** The
  operator plan's Task 19 declared it with labels `class,state` and called K.1 silent on it
  ("Ambiguity 8") while contract K.1 gives `class,priority,state`. Raised from this side; the operator
  plan has since been corrected to emit `priority`, and contract M.2's metric sweep now reports zero
  orphans in either direction. Kept here as the worked example: this repo's alerts are the CONSUMER of
  a surface another repo produces, and the only thing that catches a drifted label set is somebody
  reading both.
- 2026-07-12: **Decided NOT to extend `check_metric_provenance.py` to `dashboards/*.json` in this PR,
  after actually checking what it would take.** The structural gap is real: the checker only globs
  `alerts/*.yaml`, so a dashboard panel on a dead metric renders EMPTY, SILENTLY, with no CI signal -
  the exact same failure class this whole plan exists to kill, one surface over. Walking
  `panels[].targets[].expr` (skipping `datasource.type == "loki"` targets, same as the alert-rule
  `query_type` skip) is mechanically cheap. What is NOT cheap: doing it revealed two classes of finding
  that do not belong in a docs close-out PR. (1) ~50 metric names used across `memory.json`,
  `wrapper.json`, `ingester.json`, `quality-feedback.json` and `claude-usage-windows.json` are not on
  `scripts/metrics_allowlist.txt` - spot-verified via grep against each producing service's own
  `internal/obs`/`internal/metrics` package (`tatara-memory`, `tatara-memory-repo-ingester`,
  `tatara-claude-code-wrapper`, `tatara-operator`) and all confirmed genuinely live; this is
  mechanical allowlist backfill, not a bug, but it is ~50 lines of careful cross-repo verification.
  (2) A real bug: `dashboards/operator.json` and `dashboards/task-delivery.json` - the two dashboards
  Task 9 claims are "repointed onto the new metric surface" - still carry panels on
  `tatara_issue_state`, `tatara_issue_outcome_total`, `tatara_tasks_inflight`, `operator_open_proposals`,
  `tatara_cd_resolved_total`, `tatara_systemic_groups_led_total` and
  `tatara_systemic_siblings_collapsed_total` (verified defined in `tatara-operator/internal/obs/
  task_metrics.go` and `operator_metrics.go`, the SAME pre-redesign issue-lifecycle machine Global
  Constraint 9 says the redesign deletes). Those panels will go blank the moment the operator redesign
  lands, and fixing them means picking their K.1-surface replacements - a design call Task 9 already
  made for the panels it did touch, not a mechanical add. Recorded in full in ROADMAP.md rather than
  rushed here: landing a checker that is either disabled-by-default or immediately red on two just-
  claimed-done dashboards is worse than not landing it this PR.

## 2026-07-13 - the provenance checker now covers DASHBOARDS, and it immediately found three dead boards

`scripts/check_metric_provenance.py` walks `dashboards/*.json` as well as `alerts/*.yaml`:
`panels[].targets[].expr`, nested `panels[].panels[].targets[].expr` (row-collapsed panels), and
`templating.list[]` query variables (`label_values(<expr>, <label>)` / `query_result(...)` are
unwrapped to their PromQL; loki targets are skipped by `datasource.type`, exactly as alert rules are
skipped by `query_type`). Wired into `.github/workflows/alert-rules-lint.yml` on `dashboards/**`.
WHY: an alert on a metric nobody emits reports OK forever (`default_no_data_state: "OK"`); a DASHBOARD
panel on one renders empty, silently, forever, with not even a NoData state to mis-configure. Same
silent-green class, one surface over, and the 2026-07-12 close-out proved it live - `operator.json` and
`task-delivery.json` had been declared "repointed" while still querying `tatara_issue_state`,
`tatara_issue_outcome_total`, `tatara_tasks_inflight`, `tatara_cd_resolved_total`,
`tatara_scan_duration_seconds` and the two systemic-group counters. `task-delivery.json` was never even
in the plan's file list: a PLAN GAP, not just a missed edit.

Three things the sweep turned up that the close-out's own survey did not:
1. `tatara_scan_duration_seconds` (operator.json "Scan duration p95") is ALSO dead - the B.4 sweep
   replaces the scan loop, and K.1 retires the `tatara_scan_*` family. Repointed onto the sweep
   heartbeat (`time() - operator_sweep_last_success_timestamp_seconds`).
2. `ccw_interjections_total` (wrapper.json "Mid-Turn Interjections") is dead: the wrapper's
   task-centric branch DELETES the whole cross-pod-continuity surface and
   `cmd/wrapper/guard_no_s3_test.go` BANS the identifier from returning. Panel deleted. A blanket
   "it's live today, allowlist it" backfill would have rubber-stamped a metric whose producer is
   already gone in the sibling worktree - always check the WORKTREE, not just the pinned submodule.
3. `operator_open_proposals` is LIVE, not dead (`maxOpenProposals` is a live field on
   `BrainstormActivity`/`HealthCheckActivity`, `project_types.go:301,332`). Panel kept.

The closed-set label sweep had to become METRIC-AWARE to do this: `kind` is an overloaded label
(`operator_scm_writes_total{kind="write"}` is an access class, `operator_reconcile_total{kind}` is a CR
kind), and a metric-blind sweep flags those as illegal Task kinds. `stage_values_allowlist.txt` grew a
`## kind:exempt-metrics` section; the default stays CHECK, so a new metric that overloads a closed-set
label fails CI until someone exempts it deliberately.

2026-07-17: Added `Operator handoff drain stalled` (`alerts/tatara-operator.yaml`) for the new
`stageReason=handoff-stalled` terminal park the operator's companion PR
(`fix/review-outcome-claim-lease-and-handoff-guard`) introduces: `kind=review` is the one outcome
kind whose commit doesn't itself advance the Task's stage (that's deferred to
`MergeRequestReconciler -> DrainPendingReview -> advanceAfterReview`), and a 5m handoff deadline
now bounds that second-reconciler race. Threshold 0 / for 5m, mirrors the existing
`pod-recreation-exhausted` single-reason rule: this park means a Task whose review already landed
is now stuck with no automatic recovery but a backlog-sweep re-mint, so any occurrence pages.
Also added `handoff-stalled` to `scripts/stage_values_allowlist.txt`'s stageReason closed set -
`check_metric_provenance.py` failed on the new value until that landed, exactly the guard it exists
for. Verified `operator_task_terminal_total{kind,stage,stageReason}` and
`operator_task_parked_total{stage,stageReason}` label names against the operator worktree
(`internal/obs/task_metrics.go`) before writing the rule, not against the task's suggested
`{from,reason}` shape, which does not match current code.

CONTRACT GAPS found while repointing (recorded, not worked around):
- K.1 declares no sweep-DURATION histogram. The sweep's liveness is observable, its latency is not.
- K.1 labels `operator_task_stage_age_seconds` `{task,stage,kind}` only - no `project`/`repo`/`issue` -
  so the delivery board's per-issue table cannot be reconstructed: "Open Issues" becomes "Open Tasks by
  Stage", with no SCM deep-link and no project filter. The Task name is the only handle left.
