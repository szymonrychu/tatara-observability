resource "grafana_dashboard" "task_delivery" {
  folder      = grafana_folder.tatara.uid
  config_json = file("${path.module}/dashboards/task-delivery.json")
  overwrite   = true
}
