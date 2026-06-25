terraform {
  backend "s3" {
    bucket = "szymonrychu-terraform-state"
    key    = "terraform/tatara-observability"
    region = "eu-west-1"
  }
}
