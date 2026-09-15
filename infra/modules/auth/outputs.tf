output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.main.arn
}

output "client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "analyst_group" {
  value = aws_cognito_user_group.analyst.name
}

output "issuer" {
  value = "https://${aws_cognito_user_pool.main.endpoint}"
}
