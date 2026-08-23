# Tatara alert rules, applied to Grafana as code. Agents edit alerts/*.yaml; this renders
# each file (one rule group) into the Grafana "Tatara" folder via the grafana_alert module.
# The contact point + notification policy that route system=tatara to the operator incident
# webhook live in infra/terraform/grafana (global homelab routing, label-based, unchanged here).

resource "grafana_folder" "tatara" {
  title = "Tatara"
}

# `default_labels` is deliberately NOT wired. modules/grafana_alert/main.tf:124 is a ternary,
# not a merge: a rule's own `labels` REPLACE the default, and 130/130 rules declare their own,
# so the fallback branch was unreachable and the `homelab = "true"` that used to sit here could
# never be read. A safety net that cannot fire is worse than none - it told the next author that
# omitting homelab was survivable. scripts/check_routing_labels.py is now the single source of
# the routing contract and proves the branch stays unreachable on every PR. The module input
# itself stays declared (it is vendored from infra/terraform; keep it byte-aligned).
module "alerts" {
  source = "./modules/grafana_alert"

  alerts = [
    for f in fileset(path.module, "alerts/*.yaml") : merge({
      notification_label     = grafana_folder.tatara.uid
      folder_uid             = grafana_folder.tatara.uid
      rulegroup_name         = split(".", split("/", f)[1])[0]
      default_exec_err_state = "OK"
    }, yamldecode(file("${path.module}/${f}")))
  ]
}
