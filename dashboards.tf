resource "grafana_dashboard" "task_delivery" {
  folder      = grafana_folder.tatara.uid
  config_json = file("${path.module}/dashboards/task-delivery.json")
  overwrite   = true
}

resource "grafana_dashboard" "quality_feedback" {
  folder      = grafana_folder.tatara.uid
  config_json = file("${path.module}/dashboards/quality-feedback.json")
  overwrite   = true
}

resource "grafana_dashboard" "claude_usage_windows" {
  folder      = grafana_folder.tatara.uid
  config_json = file("${path.module}/dashboards/claude-usage-windows.json")
  overwrite   = true
}
