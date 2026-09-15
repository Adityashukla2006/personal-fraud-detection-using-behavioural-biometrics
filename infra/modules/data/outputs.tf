output "table_name" {
  value = aws_dynamodb_table.main.name
}

output "table_arn" {
  value = aws_dynamodb_table.main.arn
}

output "gsi1_name" {
  value = "GSI1"
}

output "templates_key_arn" {
  value = aws_kms_key.templates.arn
}
