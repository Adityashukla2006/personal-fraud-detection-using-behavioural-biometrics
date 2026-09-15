# Module wiring only. Resources live in modules, each owning its own IAM.

locals {
  name_prefix = "bfd"
  repo_root   = abspath("${path.root}/../../..")
}

module "observability" {
  source = "../../modules/observability"

  name_prefix      = local.name_prefix
  budget_limit_usd = var.budget_limit_usd
  alert_email      = var.alert_email
}

module "data" {
  source = "../../modules/data"

  name_prefix = local.name_prefix
}

module "storage" {
  source = "../../modules/storage"

  name_prefix = local.name_prefix
  client_dir  = "${local.repo_root}/client"

  client_config = {
    region     = var.region
    userPoolId = module.auth.user_pool_id
    clientId   = module.auth.client_id
    apiUrl     = module.api.api_url
  }
}

module "auth" {
  source = "../../modules/auth"

  name_prefix      = local.name_prefix
  relying_party_id = module.storage.cloudfront_domain
}

module "compute" {
  source = "../../modules/compute"

  name_prefix   = local.name_prefix
  table_arn     = module.data.table_arn
  table_name    = module.data.table_name
  table_key_arn = module.data.templates_key_arn
  build_dir     = "${local.repo_root}/build/lambdas"
}

module "api" {
  source = "../../modules/api"

  name_prefix         = local.name_prefix
  allowed_origins     = [module.storage.client_url]
  jwt_issuer          = module.auth.issuer
  jwt_audience        = [module.auth.client_id]
  function_name       = module.compute.scoring_function_name
  function_invoke_arn = module.compute.scoring_invoke_arn
}
