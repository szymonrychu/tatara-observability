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
