# Integration tests read every endpoint, name and ARN from here, never from hardcoded values.

output "region" {
  value = var.region
}

output "budget_name" {
  value = module.observability.budget_name
}

output "table_name" {
  value = module.data.table_name
}

output "table_arn" {
  value = module.data.table_arn
}

output "gsi1_name" {
  value = module.data.gsi1_name
}

output "templates_key_arn" {
  value = module.data.templates_key_arn
}

output "lake_bucket" {
  value = module.storage.lake_bucket
}

output "client_bucket" {
  value = module.storage.client_bucket
}

output "lake_key_arn" {
  value = module.storage.lake_key_arn
}

output "function_role_arns" {
  value = module.compute.function_role_arns
}

output "log_group_names" {
  value = module.compute.log_group_names
}
