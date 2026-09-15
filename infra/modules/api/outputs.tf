output "api_url" {
  description = "Base URL without a trailing slash."
  value       = aws_apigatewayv2_api.http.api_endpoint
}
