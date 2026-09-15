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

output "scoring_function_name" {
  value = module.compute.scoring_function_name
}

output "api_url" {
  value = module.api.api_url
}

output "client_url" {
  value = module.storage.client_url
}

output "user_pool_id" {
  value = module.auth.user_pool_id
}

output "user_pool_client_id" {
  value = module.auth.client_id
}

output "state_machine_arn" {
  value = module.workflow.state_machine_arn
}

output "alerts_topic_arn" {
  value = module.observability.alerts_topic_arn
}

output "event_bus_name" {
  value = module.events.bus_name
}

output "console_url" {
  value = "${module.storage.client_url}/console.html"
}

output "analyst_group" {
  value = module.auth.analyst_group
}
