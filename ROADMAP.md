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
- `shipped` (#111): tuned the p95 latency thresholds against real histogram buckets, and turned the
  class into a CI failure rather than a number to remember. Brainstorm issues (tatara-operator
  #145-#148) had flagged "Operator turn submit p95 latency high" at threshold 30s as unreachable -
  `operator_turn_submit_duration_seconds` tops out at 25.6s - and the census found a second, worse
  instance: "Memory LightRAG call p95 latency high" at 30s over a 10s `DefBuckets` ceiling. Both
  lowered (6.4 and 5) against live Prometheus - the operator's real p95 is flat at 0.78s over 7d, and
  no `lightrag_*` series has ever been scraped, so that one is armed blind and says so in the rule.
  The guard is `lint_alert_rules.py` Check 4 + `scripts/histogram_bounds.txt`, with
  `reconcile_metric_provenance.py` re-deriving every ceiling from producer source so the file cannot
  drift. See CONVENTIONS.md 6.4 and MEMORY.md 2026-08-16.
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
- `shipped` (2026-07-25, #63/#65/#67/#72): cadence-aware sweep heartbeat, absence-vs-zero fixes, and
  three new structural lint checks. See MEMORY.md 2026-07-25. Follow-on, still `planned`: the
  post-apply verification for this change - (1) `operator_sweep_last_success_timestamp_seconds`
  returns one series per (project, activity) with none moving backward; (2)
  `operator_sweep_next_expected_timestamp_seconds{project="tatara",activity="documentation"}` equals
  03:00 the following day; (3) "Operator sweep heartbeat stale" reads Normal through a full day
  including the ~18h window that previously fired; (4) tatara-operator#446's open question - whether
  two Projects genuinely missed a 12:00 brainstorm slot - is now answerable, because the series is
  per-Project. Pure runtime check via the grafana MCP server, no repo artefact to land. (5) Confirm
  "Tatara agent reported platform problem" actually FIRES on a real agent-reported issue after the
  operator rollout: its per-series subtraction fix (fix #71-1, see MEMORY.md 2026-07-25 final-fixes
  entry) could only be validated live against structural analogs (`agent_internal_issue_total` itself
  has zero live series today), so this is the one rule in the set whose live firing behaviour cannot
  be proven from the repo alone.
- `shipped` (2026-07-29, #81 + tatara-documentation#24): every alert rule carries a
  `runbook_url` pointing at a rule-name-derived anchor on the published runbooks page, plus
  `scripts/check_runbook_urls.py` in `alert-rules-lint.yml` (exact-URL gate locally, anchor
  existence by shallow-cloning tatara-documentation, coverage printed to the job summary) and
  the reciprocal append-only anchor guard in tatara-documentation. CONVENTIONS.md section 8
  is the author-facing contract. See MEMORY.md 2026-07-29. This closes #79's action item 4
  (a `runbookURL` on `cfrz32fd0veo0a`) as a side effect - that rule is covered like every other.
- `planned`: the measurable test of whether this actually changed behaviour is the NEXT firing
  of `Memory postgres or neo4j container stuck waiting` (rule key 944c6a861f89f080). It has
  produced three full from-scratch RCAs (tatara-helmfile #245, #263, #237) whose root cause was
  already one of two documented bullets on the runbook page. A fourth from-scratch RCA means the
  incident agent is reading the link and ignoring it, which is a `tatara-agent-skills` problem,
  not an alerting one. Check this before adding any further runbook prose.
- `external`, BLOCKS THIS REPO BUT CANNOT BE FIXED HERE (tatara-observability#79 root cause (a)):
  the promtail DaemonSet in namespace `monitoring` runs on 3 of the cluster's 5 Ready nodes -
  `kube_daemonset_status_desired_number_scheduled{daemonset="promtail"}` is 3, while
  `prometheus-prometheus-node-exporter` and `smartctl-exporter-*` are both 5 on the same cluster,
  so this is promtail's OWN nodeSelector/tolerations excluding `nas-d0w363i` and `worker-jtw3f33`,
  not a scheduling outage. `list_loki_label_values(node_name)` over 7d confirms neither node has
  ever shipped a line. Consequence for every Loki rule in `alerts/tatara-logs.yaml`: an empty
  result for a pod on those two nodes means "not collected", never "no output". The collector is
  not deployed by any tatara-* repo (`tatara-helmfile` ships only the `tatara`-namespace app
  releases), so the fix belongs to the monitoring-stack owner: give the promtail DaemonSet the
  tolerations matching those two nodes' taints (node-exporter's toleration set is the working
  reference on this same cluster) and/or drop its restricting nodeSelector, then re-check that
  `list_loki_label_values(node_name)` returns 5 values. This repo's half is shipped: the "Log
  collector node coverage incomplete" rule now fires (currently = 2) so the blind spot is visible
  instead of silent. Retire this line when that expression reads 0.
- `shipped` (2026-07-29, this branch + tatara-documentation `fix/observability-followups`):
  integration of #86 and #85 plus `alerts/tatara-nodes.yaml`, a new rule group carrying
  "Node pod network partitioned" and "Node volume plane wedged", both routed from
  tatara-helmfile#294 (closing its #239 and #245) and both re-verified against live
  Prometheus before landing: the partition ratio's healthy baseline is 0.09-0.28 across
  all 5 nodes so `> 0.8` has real headroom, and the volume gap is 0 on every node at every
  15m sample over 24h except a single instantaneous 1 on `worker-jtw3f33` (a mount in
  progress, which is exactly what `for: 15m` absorbs). New file, no terraform change:
  `grafana.tf` discovers rule groups with `fileset(path.module, "alerts/*.yaml")`.
- `blocked on producer PRs, DO NOT ALLOWLIST YET`: six open PRs across the sweep introduce
  metrics this repo will want. `scripts/reconcile_metric_provenance.py` hard-fails on an
  allowlist entry no producer **main** emits and runs on every PR and push (proven
  empirically 2026-07-29, see MEMORY.md), so each name below lands in
  `scripts/metrics_allowlist.txt` only once its PR merges. All were verified in producer
  Go source at the PR head - name, type and labels - so each is a one-line addition then,
  not a re-investigation. NONE is emitted by the deployed operator (v1.35.1) today, so no
  rule may key on any of them yet either: `alerts/tatara-operator.yaml` sets
  `default_no_data_state: "OK"`, which would make such a rule silently green - the exact
  failure class `check_metric_provenance.py` exists to prevent.
  - tatara-operator#485: `operator_stage_race_lost_total{from,to}` (counter,
    `internal/obs/stage_metrics.go`).
  - tatara-operator#487: `operator_memory_apply_transient_errors_total{project}` (counter,
    `internal/obs/operator_metrics.go`). The only signal for the newly-absorbed
    transient-webhook window.
  - tatara-operator#489: `operator_sweep_skipped_total{project,activity,reason}` (counter,
    `internal/obs/sweep_metrics.go`; only reason value today is
    `mr_claimed_by_other_task`). NOT an error signal - see MEMORY.md.
  - tatara-operator#490: `operator_fold_in_flight_blocked_tasks{project}` (GAUGE, one
    label, `internal/obs/reaper_metrics_v2.go`). Aggregate with `max by (project)`, never
    `sum` - it is per-replica and summing would triple-count on 3 replicas.
  - tatara-claude-code-wrapper#141: `ccw_bootstrap_reconcile_total{result}` (result values
    `up_to_date|merged|conflict|fetch_fail|base_unresolved`) and
    `ccw_commit_oversized_blob_skipped_total`, which is a plain Counter with NO labels -
    any selector with a label matcher on it matches nothing.
  - tatara-memory#97: `http_admission_in_flight{class}`, `http_admission_waiting{class}`,
    `http_admission_total{class,result}`, `http_admission_wait_seconds{class,result}`
    (`internal/httpapi/admission.go`; class `memories_bulk|code_graph_bulk`, result
    `admitted|shed|canceled`).
  - tatara-memory#94: no new hand-declared family. It registers
    `collectors.NewDBStatsCollector(db, "tatara_memory")`, which exposes nine `go_sql_*`
    names each carrying only `db_name`, and adds the label VALUE
    `code_graph_analytics_runs_total{result="timeout"}` to an existing family.
  - tatara-memory#92: no new family either - it widens `tatara_memory_op_total` from
    {op,result} to {op,class,result}. Already relied on by "Memory service operation error
    ratio high" and safe ahead of the merge (negative matcher on an absent label).
  - tatara-memory-repo-ingester#32: `code_graph_batches_total{result}`,
    `code_graph_batch_rows` (histogram, no labels), `push_retries_total{path,reason}`,
    `push_shed_responses_total{path,status}` (`internal/obs/obs.go`). CARDINALITY HAZARD
    to settle before alerting on the last two: `path` is the raw request path and
    `internal/push/push.go` passes `"/ingest-jobs/"+job.ID`, so a retry or shed on job
    polling emits an unbounded per-job-ID label value. Raise it on that PR.
- `routed to tatara-operator, producer-side instrumentation gap` (2026-07-29): the
  operator logs ERROR lines the sweep counter does not count.
  `increase(operator_sweep_errors_total{reason="reconcile_ownership"}[2h])` returned no
  series above zero at the same moment the Loki rule "Tatara operator error recurring" was
  firing on 3 ERROR lines with msg="sweep: reconcile_ownership" in 1h. Until that is
  closed, the Loki rule is the ONLY signal for those failures, which is why this branch
  refused to re-key it onto Prometheus sweep metrics (MEMORY.md 2026-07-29). Re-check
  after the fix; the Loki rule can be narrowed only once the counter demonstrably covers
  what the log lines report.
