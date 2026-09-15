data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

output "workgroup_name" {
  value = aws_athena_workgroup.batch.name
}

output "workgroup_arn" {
  value = aws_athena_workgroup.batch.arn
}

output "database_name" {
  value = aws_glue_catalog_database.lake.name
}

output "table_name" {
  value = aws_glue_catalog_table.events.name
}

# Glue authorises a table read against the catalog, the database and the table together.
output "glue_resource_arns" {
  value = [
    "arn:aws:glue:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:catalog",
    aws_glue_catalog_database.lake.arn,
    aws_glue_catalog_table.events.arn,
  ]
}
