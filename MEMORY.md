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
