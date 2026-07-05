# MEMORY.md - tatara-observability

Past decisions + context. One dated line per entry.

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
