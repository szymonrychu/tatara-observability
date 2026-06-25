variable "grafana_url" {
  description = "Url to grafana"
  type        = string
  nullable    = false
}

variable "grafana_api_key" {
  description = "Grafana Editor service-account token (writes alert rules in the Tatara folder)"
  type        = string
  nullable    = false
  sensitive   = true
}
