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
