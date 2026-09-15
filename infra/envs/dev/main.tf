# Module wiring only. Resources live in modules, each owning its own IAM.

module "observability" {
  source = "../../modules/observability"

  name_prefix      = "bfd"
  budget_limit_usd = var.budget_limit_usd
  alert_email      = var.alert_email
}

module "data" {
  source = "../../modules/data"

  name_prefix = "bfd"
}

module "storage" {
  source = "../../modules/storage"

  name_prefix = "bfd"
}

module "compute" {
  source = "../../modules/compute"

  name_prefix = "bfd"
  table_arn   = module.data.table_arn
}
