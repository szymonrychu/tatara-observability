# Tatara alert rules, applied to Grafana as code. Agents edit alerts/*.yaml; this renders
# each file (one rule group) into the Grafana "Tatara" folder via the grafana_alert module.
# The contact point + notification policy that route system=tatara to the operator incident
# webhook live in infra/terraform/grafana (global homelab routing, label-based, unchanged here).

locals {
  # Default labels merged into rules that do not set their own `labels`. Every tatara rule sets
  # its own labels (homelab+system=tatara+component+severity), which REPLACE these; homelab=true
  # is kept here so the parent homelab notification policy matches.
  alert_tags = {
    homelab = "true"
  }
}

resource "grafana_folder" "tatara" {
  title = "Tatara"
}

module "alerts" {
  source = "./modules/grafana_alert"

  alerts = [
    for f in fileset(path.module, "alerts/*.yaml") : merge({
      notification_label     = grafana_folder.tatara.uid
      folder_uid             = grafana_folder.tatara.uid
      default_labels         = local.alert_tags
      rulegroup_name         = split(".", split("/", f)[1])[0]
      default_exec_err_state = "OK"
    }, yamldecode(file("${path.module}/${f}")))
  ]
}
