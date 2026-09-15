output "function_role_arns" {
  value = { for name, role in aws_iam_role.function : name => role.arn }
}

output "log_group_names" {
  value = { for name, group in aws_cloudwatch_log_group.function : name => group.name }
}

output "scoring_function_name" {
  value = aws_lambda_function.scoring.function_name
}

output "scoring_invoke_arn" {
  value = aws_lambda_function.scoring.invoke_arn
}

output "ledger_function_arn" {
  value = aws_lambda_function.ledger.arn
}

output "transfers_function_name" {
  value = aws_lambda_function.transfers.function_name
}

output "transfers_invoke_arn" {
  value = aws_lambda_function.transfers.invoke_arn
}

output "adaptation_function_arn" {
  value = aws_lambda_function.adaptation.arn
}

output "adaptation_function_name" {
  value = aws_lambda_function.adaptation.function_name
}

output "archive_function_arn" {
  value = aws_lambda_function.archive.arn
}

output "archive_function_name" {
  value = aws_lambda_function.archive.function_name
}

output "console_function_name" {
  value = aws_lambda_function.console.function_name
}

output "console_invoke_arn" {
  value = aws_lambda_function.console.invoke_arn
}
