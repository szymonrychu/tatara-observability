# ROADMAP.md - tatara-observability

Planned work not yet started. Move items out when shipped (note in MEMORY.md if non-obvious).

- `planned`: dashboards-as-code (per-component Grafana dashboards) once alerts are settled.
- `planned`: scope the Grafana SA token to Editor + (if Grafana supports it) the `Tatara` folder
  only, instead of a broad Editor token.
- `planned`: wire the `mem-*` per-project memory pods + the Argo workflow-controller to Prometheus
  scrape (Gap 1 / Gap 2 from the alerting design) so the currently-dark memory app-metric rules and
  a precise argo CI rule (`argo_workflows_count`) can fire.
- `planned`: re-add an argo CI alert here once `argo_workflows_count` is scraped (the original
  namespace-wide Failed-pod proxy was dropped as too noisy).
- `planned`: tune the p95 latency thresholds against real histogram buckets. Brainstorm issues
  (tatara-operator #145-#148) flagged "Operator turn submit p95 latency high" at threshold 30s is
  unreachable because the `operator_turn_submit_duration_seconds` histogram tops out at ~25.6s, so
  after the NaN-guard fix the rule is inert (never fires). Lower the threshold below the bucket
  ceiling (and confirm buckets per component) so it can fire on genuine slowness; same check for the
  memory/chat p95 rules.
