# ROADMAP.md - tatara-observability

Planned work not yet started. Move items out when shipped (note in MEMORY.md if non-obvious).

- `shipped`: dashboards-as-code bootstrap - "Tatara - Task Delivery" dashboard (feat/task-delivery-dashboard). See MEMORY.md 2026-06-27.
- `shipped`: semver push-CD cascade alerts - `alerts/tatara-cd.yaml` (`tatara_cd_cascade_failed`/`_stalled`). See MEMORY.md 2026-06-28. Depends on the operator G5 metrics landing; rules stay inert (`or vector(0)` -> 0) until then.
- `planned`: scope the Grafana SA token to Editor + (if Grafana supports it) the `Tatara` folder
  only, instead of a broad Editor token.
- `shipped` (Gap 1, confirmed 2026-07-05): the `mem-*` per-project memory pods ARE scraped
  (`job="tatara-memory"`, `service="mem-<project>"` label from the Service object) - verified live
  against Grafana/Prometheus (see MEMORY.md 2026-07-05, dashboard consolidation entry). This item
  was stale; only Gap 2 remains.
- `planned` (Gap 2): wire the Argo workflow-controller to Prometheus scrape so a precise argo CI rule
  (`argo_workflows_count`) can fire.
- `shipped` (2026-07-05): consolidated the monitoring-audit workflow's duplicate dashboards (operator
  x3, wrapper/memory/chat x2 each) into one canonical board apiece - `operator.json` (47p),
  `wrapper.json` (27p), `memory.json` (32p), `chat.json` (16p) - plus new `ingester.json` (10p) and
  `agent-lifecycle.json` (9p); deleted the 9 redundant JSONs; and wired ALL of
  operator/wrapper/memory/chat/ingester/agent-lifecycle into `dashboards.tf` (previously only
  task_delivery/quality_feedback/claude_usage_windows had resources, so every other dashboard JSON was
  dead/unapplied). See MEMORY.md 2026-07-05.
- `shipped` (follow-up from #19, D2): closed the latent probe false-positive on "Memory HTTP 5xx
  error ratio high" consumer-side (#21) - added `route!~"/readyz|/healthz|/metrics"` to both selectors
  and dropped the rule's `tatara_probe_exclusion` KNOWN-GAP annotation. tatara-memory's
  `http_requests_total` carries a chi `route` label (verified `tatara-memory/internal/httpapi/middleware.go`),
  so the exclusion is exact, not a guess. The producer-side variant (mount /healthz,/readyz OUTSIDE
  metrics.Middleware in `tatara-memory/internal/httpapi/router.go:41,43-44`) is now an OPTIONAL cleanup
  follow-up in tatara-memory, no longer a prerequisite for enabling mem-* scrape. See CONVENTIONS.md.
- `planned`: re-add an argo CI alert here once `argo_workflows_count` is scraped (the original
  namespace-wide Failed-pod proxy was dropped as too noisy).
- `planned`: tune the p95 latency thresholds against real histogram buckets. Brainstorm issues
  (tatara-operator #145-#148) flagged "Operator turn submit p95 latency high" at threshold 30s is
  unreachable because the `operator_turn_submit_duration_seconds` histogram tops out at ~25.6s, so
  after the NaN-guard fix the rule is inert (never fires). Lower the threshold below the bucket
  ceiling (and confirm buckets per component) so it can fire on genuine slowness; same check for the
  memory/chat p95 rules.
- `shipped` (Task 6, G5): tier-quality rubber-stamp alert rule in `alerts/tatara-quality.yaml`
  ("Tier-quality rubber-stamp (model=claude-sonnet-5)"), labels `homelab`/`system=tatara`/
  `tatara_tier_quality=true`/`kind=review`/`model=claude-sonnet-5`/`project=tatara`. See MEMORY.md
  2026-07-04. `planned` follow-up: tune the 0.02 find-rate threshold and 5-review min-volume gate
  against the real G4 baseline once claude-sonnet-5 review data accumulates; add the optional
  implement-CI-pass-rate floor rule once CI attribution is trusted.
- `shipped` (Phase C, claude-subscription-usage-gate): "Tatara - Claude Usage Windows" dashboard
  (`dashboards/claude-usage-windows.json`) + `alerts/tatara-usage-gate.yaml` (poll-health, emergency-
  ceiling, 429 backstop, overage-climbing). See MEMORY.md 2026-07-04. `planned` follow-up: (1) once
  the operator poller (Phase A) deploys, confirm `tatara_account_usage_*`/`operator_admission_blocked_
  total{reason="kind_ceiling"}` panels populate as expected and tune the 80% emergency-ceiling
  threshold against real utilization; (2) once the OTLP->Prometheus collector deploys (Phase D,
  tatara-helmfile), confirm the exact scraped names for `claude_code_cost_usage` /
  `claude_code_api_error{status_code}` and fix the two OTel panels + the 429 alert rule if the
  collector's naming differs from the plan's assumed Prometheus-normalized form.
- `shipped` (2026-07-12): the observability half of the task-centric redesign
  (`docs/superpowers/plans/2026-07-12-task-centric-observability.md`). `tatara-cd.yaml` (6 rules) and
  `tatara-operator.yaml` (52 rules) re-expressed on the stage/park metric surface; `tatara-chat.yaml` +
  `dashboards/chat.json` + their terraform resource + the chat log-burst rule deleted;
  `dashboards/operator.json` + `dashboards/agent-lifecycle.json` repointed;
  `scripts/check_metric_provenance.py` + `scripts/metrics_allowlist.txt` +
  `scripts/stage_values_allowlist.txt` wired into CI as a new build-failure guardrail. See MEMORY.md
  2026-07-12 (the dead-alert class entry) for the full account.
- `planned`, OPEN before the release train ships (D1, see MEMORY.md 2026-07-12): re-verify
  `operator_task_terminal_total` survives the operator redesign with `phase` swapped for `stage` +
  `stageReason` + `kind`. As of this PR the operator worktree still declares the OLD `{kind,phase,reason}`
  label set (`internal/obs/task_metrics.go:31-33`) - several `tatara-cd.yaml`/`tatara-operator.yaml`
  rules and both repointed dashboards are built on an UNVERIFIED assumption. Re-run Task 2 step 3's
  grep against the actual merged operator branch before cutover; if the metric or its labels differ,
  every rule built on it changes in the same PR (Task 6's documented fallback).
- `planned`: threshold/config coupling not enforced by anything mechanical - the agent-pod saturation
  threshold (`alerts/tatara-operator.yaml`, `5.999` = `2 x maxConcurrentAgents`) hardcodes
  `tatara-helmfile`'s `maxConcurrentAgents: 3` on both Projects. Bumping `maxConcurrentAgents` in
  helmfile REQUIRES bumping this threshold in the SAME change, or the rule silently stops meaning what
  its summary claims. No CI check ties the two repos together on this value; consider one if
  `maxConcurrentAgents` starts changing often.
- `planned`: `alerts/tatara-quality.yaml` and `alerts/tatara-usage-gate.yaml` were not audited against
  the task-centric redesign (Task 10 follow-up). They read token/model series, which are unchanged, but
  nobody has confirmed the `tatara-quality` rubber-stamp rule still has a producer once the review
  verdict moves to `submit_outcome` (contract). Audit both files against the merged operator branch in
  the same pass as the D1 re-verification above.
- `shipped` (2026-07-13): DASHBOARD-SIDE half of the metric-provenance guardrail.
  `scripts/check_metric_provenance.py` now walks `dashboards/*.json` as well as `alerts/*.yaml`
  (panel targets, row-collapsed sub-panel targets, and `templating.list[]` PromQL variables; loki
  targets skipped by `datasource.type`), the closed-set label sweep is now metric-aware, the ~50
  live dashboard-only metric names are backfilled into `scripts/metrics_allowlist.txt` per producing
  service, and the dead-metric panels in `operator.json` / `task-delivery.json` / `wrapper.json` are
  repointed or deleted. CI runs it on `dashboards/**` too. See MEMORY.md 2026-07-13.
- `planned`: run the post-apply verification in
  `docs/superpowers/plans/2026-07-12-task-centric-observability.md` ("Post-apply verification
  (cutover step 8c, contract H.3)") after `tatara-helmfile` applies the new operator. Pure runtime
  check via the Grafana MCP server (`grafana-debugging-start`) - no repo artefact to land, nothing to
  build now: (1) every rule in `tatara-operator`/`tatara-cd`/`tatara-logs` must read `Normal` or
  `Firing`, never silently `NoData`; (2) `Operator sweep heartbeat stale` must be `Normal`, not
  `NoData`; (3) `sum(increase(operator_agent_contract_mismatch_total[1h]))` must be `0`. A `NoData`
  hit on a K.1 metric means the operator PR is incomplete, not that this repo's alerts are wrong.
- `shipped` (2026-07-18, issue #57): reverse-drift metric provenance reconciliation.
  `scripts/reconcile_metric_provenance.py` shallow-clones the 4 producer repos nightly + on PR/push and
  fails when the allowlist carries a name none of them emit any more - the hole `check_metric_
  provenance.py` never covered (it only validates alerts/dashboards against the allowlist, not the
  allowlist against reality). See MEMORY.md 2026-07-18.
- `planned`: audit the remaining `alerts/*.yaml` files (`tatara-cd.yaml`, `tatara-ingester.yaml`,
  `tatara-memory.yaml`, `tatara-operator.yaml`, `tatara-quality.yaml`, `tatara-usage-gate.yaml`,
  `tatara-wrapper.yaml`) for the same OK/OK NoData+ExecErr blindness `tatara-logs.yaml` just fixed
  (tatara-operator#381 stream 1, see MEMORY.md 2026-07-19) - flip `default_no_data_state`/
  `default_exec_err_state` to `Alerting` per file where a Loki-backend-outage should page rather than
  go quiet, and add `or vector(0)` guards to whichever count-style expressions need them in the same
  change. Deferred out of this PR for diff reviewability - one file at a time, not a repo-wide sweep.
