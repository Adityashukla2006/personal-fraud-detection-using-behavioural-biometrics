output "function_role_arns" {
  value = { for name, role in aws_iam_role.function : name => role.arn }
}

output "log_group_names" {
  value = { for name, group in aws_cloudwatch_log_group.function : name => group.name }
}
