# Module wiring only. Resources live in modules, each owning its own IAM.

module "observability" {
  source = "../../modules/observability"

  name_prefix      = "bfd"
  budget_limit_usd = var.budget_limit_usd
  alert_email      = var.alert_email
}
