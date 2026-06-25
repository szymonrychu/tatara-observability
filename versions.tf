terraform {
  required_version = ">= 1.0.0, < 2.0.0"
  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "4.39.0"
    }
  }
}
