locals {
  # below are the models for each of the supported datasources
  # each of the models have it's own unique set of keys, that
  # are necessary to be filled properly in order for the datasource
  # to work properly for specified query
  default_prometheus_model = {
    refId         = "" # needs to be filled in runtime
    datasource    = {} # needs to be filled in runtime
    expr          = "" # needs to be filled in runtime
    editorMode    = "code"
    instant       = true
    intervalMs    = 1000
    legendFormat  = "__auto"
    maxDataPoints = 43200
    range         = false
  }
  default_loki_model = {
    refId         = "" # needs to be filled in runtime
    datasource    = {} # needs to be filled in runtime
    expr          = "" # needs to be filled in runtime
    editorMode    = "code"
    instant       = true
    intervalMs    = 1000
    legendFormat  = "__auto"
    maxDataPoints = 43200
    range         = false
    queryType     = "instant"
  }
  default_math_model = {
    refId      = "" # needs to be filled in runtime
    expression = "" # needs to be filled in runtime
    conditions = [
      {
        type = "query"
        evaluator = {
          params = [0, 0]
          type   = "gt"
        }
        operator = {
          type = "and"
        }
        query = {
          params = []
        }
        reducer = {
          params = []
          type   = "avg"
        }
      }
    ]
    intervalMs    = 1000
    maxDataPoints = 43200
    hide          = false
    type          = "math"
  }

  default_reduce_model = {
    refId      = "" # needs to be filled in runtime
    expression = "" # needs to be filled in runtime
    conditions = [
      {
        type = "query"
        evaluator = {
          params = []
          type   = "gt"
        }
        operator = {
          type = "and"
        }
        query = {
          params = []
        }
        reducer = {
          params = []
          type   = "last"
        }
      }
    ]
    intervalMs    = 1000
    maxDataPoints = 43200
    type          = "reduce"
    reducer       = "last"
  }

  # below is a static map used for picking up proper model based on the
  # datasource type chosen for the query
  type_to_model = {
    "prometheus" = local.default_prometheus_model
    "loki"       = local.default_loki_model
    "math"       = local.default_math_model
  }

  # static list of ref_IDs to work with
  # it's really not that important what's in the strings,
  # as long as the strings are unique and can be interpolated using "$<something>"
  # in the query
  ref_ids = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z"]
}

resource "grafana_rule_group" "rules" {
  for_each = {
    for a in var.alerts : a.rulegroup_name => a
  }

  name             = each.value.rulegroup_name
  folder_uid       = each.value.folder_uid
  interval_seconds = each.value.interval_seconds

  dynamic "rule" {
    for_each = each.value.rules

    content {
      name           = rule.value.name
      condition      = element(local.ref_ids, length(rule.value.queries) + 2)
      no_data_state  = coalesce(rule.value.no_data_state, each.value.default_no_data_state)
      exec_err_state = coalesce(rule.value.exec_err_state, each.value.default_exec_err_state)
      for            = rule.value.for
      annotations    = rule.value.annotations
      # some alerts had the labels extended with {"":""}, therefore I've added it as well
      labels    = length(rule.value.labels) > 0 ? rule.value.labels : each.value.default_labels
      is_paused = rule.value.is_paused

      # create first few "data" blocks dynamically with queries provided by the user
      dynamic "data" {
        # in for_each use dynamic for block and in the loop use
        # rule.value.queries if it's not empty, else build the array
        # manually from single query provided by the user
        for_each = [
          for index, query in rule.value.queries : {
            expression               = query.expression
            relative_time_range_from = query.relative_time_range_from
            relative_time_range_to   = query.relative_time_range_to
            query_type               = query.query_type
            ref_id                   = element(local.ref_ids, index),
            # pick override datasource if available for this query, else use global one
            datasource_uid = query.datasource_uid != null ? query.datasource_uid : each.value.default_datasource_uid
          }
        ]

        content {
          ref_id = data.value.ref_id
          # Grafana persists the AlertQuery `queryType` at the data-block level too, mirroring
          # the model's `queryType` (loki -> "instant"; prometheus/math have none). Source it
          # from the same model map so the block and the model never disagree. Without this the
          # loki blocks perpetually plan `query_type: "instant" -> null`, force-updating the
          # tatara-logs group on every apply - and that needless write against the slow Grafana
          # provisioning API is what hit context-deadline-exceeded post-merge (issue #8).
          query_type = lookup(local.type_to_model[data.value.query_type], "queryType", null)
          relative_time_range {
            from = data.value.relative_time_range_from
            to   = data.value.relative_time_range_to
          }
          datasource_uid = data.value.datasource_uid
          # for each key in the base model template, use the runtime override if present,
          # otherwise fall back to the template value — this keeps the JSON clean by
          # excluding keys (e.g. `expr` for math, `expression` for prometheus) that
          # don't belong to the given query type
          model = jsonencode({
            for k, v in local.type_to_model[data.value.query_type] : k => lookup(
              {
                refId = data.value.ref_id
                datasource = data.value.query_type == "math" ? {
                  type = "__expr__"
                  uid  = "__expr__"
                  } : {
                  type = data.value.query_type
                  uid  = data.value.datasource_uid
                }
                expr       = contains(["prometheus", "loki"], data.value.query_type) ? data.value.expression : ""
                expression = data.value.query_type == "math" ? data.value.expression : ""
              },
              k, v
            )
          })
        }
      }

      data {
        ref_id         = element(local.ref_ids, length(rule.value.queries))
        datasource_uid = -100
        # let's reduce last query from the ones coming from input (pick last one from the array of values computed above)
        model = jsonencode(merge(local.default_reduce_model, {
          refId      = element(local.ref_ids, length(rule.value.queries))
          expression = element(local.ref_ids, length(rule.value.queries) - 1)
          datasource = {
            type = "__expr__"
            uid  = "__expr__"
          }
        }))
        relative_time_range {
          from = 0
          to   = 0
        }
      }

      data {
        ref_id         = element(local.ref_ids, length(rule.value.queries) + 1)
        datasource_uid = -100
        # let's round previous value to specified decimal point
        model = jsonencode(merge(local.default_math_model, {
          # in here we need to create a "$${}" in a string, that will result in "$B" after resolution
          # unfortunately "$${}" is an escape sequence reserved in terraform, therefore we need to workaround that
          # the query rounds the amount of numbers after the comma (reduces decimal points), so the alerts aren't triggerring if query gets above the threshold over 0.00001
          expression = format("round($%s * %d) / %d", element(local.ref_ids, length(rule.value.queries)), pow(10, rule.value.decimal_points), pow(10, rule.value.decimal_points))
          refId      = element(local.ref_ids, length(rule.value.queries) + 1)
          datasource = {
            type = "__expr__"
            uid  = "__expr__"
          }
        }))
        relative_time_range {
          from = 0
          to   = 0
        }
      }

      data {
        # if there are multiple quries provided, use the lenght of the queries array to compute next ref_id
        # else the rule is simpler and there are three preceding queries that we care about
        ref_id         = element(local.ref_ids, length(rule.value.queries) + 2)
        datasource_uid = -100
        relative_time_range {
          from = 0
          to   = 0
        }
        model = jsonencode(merge(local.default_math_model, {
          # in here we need to create a "$${}" in a string, that will result in "$B" after resolution
          # unfortunately "$${}" is an escape sequence reserved in terraform, therefore we need to workaround that
          expression = format("$%s %s %f", element(local.ref_ids, length(rule.value.queries) + 1), rule.value.math_operator, rule.value.threshold)
          refId      = element(local.ref_ids, length(rule.value.queries) + 2)
          datasource = {
            type = "__expr__"
            uid  = "__expr__"
          }
        }))
      }
    }
  }
}
