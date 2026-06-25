

variable "alerts" {
  description = "List of JSON strings"
  nullable    = false
  type = list(object({
    rulegroup_name         = string
    default_labels         = optional(map(string), {}),
    folder_uid             = string
    interval_seconds       = optional(number, 60)
    default_datasource_uid = optional(string, "prometheus"),
    default_query_type     = optional(string, "prometheus"),
    default_no_data_state  = optional(string, "NoData"),
    default_exec_err_state = optional(string, "Error"),
    rules = list(object({
      name = string,
      queries = list(object({
        expression     = optional(string, ""),
        datasource_uid = optional(string),
        query_type     = optional(string, "prometheus"),
        # allow to setup alerts for rarely updated metrics
        # e.g. metrics updated by-daily can set
        #   relative_time_range_from=604800
        # so analysis will go back into week in seconds
        relative_time_range_from = optional(number, 1200),
        relative_time_range_to   = optional(number, 0),
      })),
      math_operator  = optional(string, ">"),
      threshold      = number
      for            = optional(string, "1m"),
      decimal_points = optional(number, 2),
      annotations    = optional(map(string), {}),
      labels         = optional(map(string), {}),
      no_data_state  = optional(string),
      exec_err_state = optional(string),
      is_paused      = optional(bool, false)
    }))
  }))
}
