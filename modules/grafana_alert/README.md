# grafana_alert submodule

Creates `grafana_rule_group` resources from a structured list of alert definitions. Each alert definition maps to one alert rule with a multi-stage query pipeline.

## Query pipeline

For each rule, the module builds the following query chain:

1. **Prometheus queries** — one or more user-defined PromQL expressions
2. **Reduce** — takes the last value of each query result
3. **Round** — rounds to `decimal_points` precision
4. **Threshold** — fires when the rounded value compares against `threshold` via `math_operator` (default: `<`)

## Input variable: `alerts`

A list of alert group objects. Each object corresponds to one `grafana_rule_group`.

Top-level keys per object:

| Key | Required | Description |
|-----|----------|-------------|
| `rulegroup_name` | yes | Rule group name (set automatically from filename by parent module) |
| `folder_uid` | yes | Grafana folder UID (set by parent module) |
| `rules` | yes | List of alert rule definitions |
| `default_labels` | no | Labels applied to all rules in the group |
| `default_exec_err_state` | no | State on execution error (`OK` default) |
| `notification_label` | no | Label for notification routing |

Each entry in `rules`:

| Key | Required | Description |
|-----|----------|-------------|
| `name` | yes | Alert rule name |
| `queries` | yes | List of `{ expression: "..." }` PromQL queries |
| `threshold` | yes | Numeric threshold for firing |
| `for` | yes | Duration before alert fires (e.g. `300s`) |
| `decimal_points` | no | Rounding precision (default: `0`) |
| `math_operator` | no | Comparison operator (default: `< 0`) |
| `annotations` | no | Map of annotation key → template string |
| `labels` | no | Map of extra labels for this rule |

## Usage

Called from the parent `grafana/grafana_alerts.tf` — loads all `alerts/*.yaml` files:

```hcl
module "alerts" {
  source = "./modules/grafana_alert"

  alerts = [
    for f in fileset(path.module, "alerts/*.yaml") : merge({
      notification_label     = grafana_folder.default.uid
      folder_uid             = grafana_folder.default.uid
      rulegroup_name         = split(".", split("/", f)[1])[0]
      default_exec_err_state = "OK"
    }, yamldecode(file("${path.module}/${f}")))
  ]
}
```

## Provider requirements

Requires `grafana/grafana` >= 1.28.2.
