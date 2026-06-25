# tatara-observability

Observability-as-code for the [tatara](https://github.com/szymonrychu) platform:
agent-adjustable Grafana **alert rules**, applied to Grafana by terraform on merge.

## How agents (and humans) change an alert

Edit a file under `alerts/` and open a PR. That's it - you do not touch terraform.

Each `alerts/tatara-<component>.yaml` is one Grafana rule group. A rule:

```yaml
interval_seconds: 60
default_no_data_state: "OK"
rules:
  - name: "Operator reconcile error ratio high"
    queries:
      - expression: |
          sum(rate(operator_reconcile_total{result="error"}[10m])) / clamp_min(sum(rate(operator_reconcile_total[10m])), 1)
        # datasource_uid + query_type are optional; default prometheus.
        # For Loki/LogQL rules: datasource_uid: "efihqbqlmroqod"  query_type: "loki"
    math_operator: ">"          # how `threshold` is compared
    threshold: 0.2
    for: 15m
    decimal_points: 2
    annotations:
      summary: "..."            # may use {{ index $values "C" }} and {{ index $labels "x" }}
    labels:                     # REPLACES the module defaults - set all four:
      homelab: "true"           # matches the homelab notification policy
      system: "tatara"          # routes to the operator incident webhook (omit on info rules)
      component: "operator"
      severity: "warning"       # warning|critical trigger an incident; info -> email only
```

`expression` is the VALUE; the comparison is `math_operator` + `threshold`. The module
builds the reduce -> round -> threshold chain. `up == 0` becomes `expression: sum(up{...})`,
`math_operator: "<"`, `threshold: 1`.

## CI

GitHub Actions (`.github/workflows/apply.yml`): PR -> `terraform plan` (sticky comment);
merge to `main` -> `terraform apply` into the Grafana `Tatara` folder.

Required GitHub Actions secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (S3 state),
`TF_VAR_GRAFANA_API_KEY` (Grafana Editor SA token), `TF_VAR_GRAFANA_URL`.

## Boundary

Contact points + the `system=tatara` notification policy live in `infra/terraform/grafana`
(global homelab routing). This repo owns only the `Tatara` folder + the `tatara-*` rule
groups. See `docs/superpowers/specs/2026-06-25-tatara-observability-migration-design.md` in
the `tatara` repo.
