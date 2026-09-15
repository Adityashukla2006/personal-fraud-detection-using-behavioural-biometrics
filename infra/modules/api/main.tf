# HTTP API in front of the Lambda functions: JWT validation and throttling at the edge, so no
# function runs for unauthenticated or flooding traffic (architecture section 11.1).

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

resource "aws_apigatewayv2_api" "http" {
  name          = "${var.name_prefix}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = var.allowed_origins
    allow_methods = ["GET", "POST"]
    allow_headers = ["authorization", "content-type"]
    # Lets the client read the handler's own timing and separate it from network latency.
    expose_headers = ["server-timing"]
    max_age        = 3600
  }
}

resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.http.id
  name             = "${var.name_prefix}-cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = var.jwt_issuer
    audience = var.jwt_audience
  }
}

resource "aws_apigatewayv2_integration" "function" {
  for_each = var.functions

  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = each.value.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 10000
}

# Every route requires a valid Cognito token.
resource "aws_apigatewayv2_route" "route" {
  for_each = var.routes

  api_id             = aws_apigatewayv2_api.http.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.function[each.value].id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

# HTTP APIs throttle per stage and per route, not per caller: per-client usage plans exist only on
# REST APIs. This is a ceiling on total traffic, which bounds cost under a flooding simulator; it
# does not isolate one user from another.
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = var.throttling_burst_limit
    throttling_rate_limit  = var.throttling_rate_limit
  }
}

# Invoke permissions belong to the API because the API is the caller they authorise. Each is scoped
# to the function's own path.
resource "aws_lambda_permission" "function" {
  for_each = var.functions

  statement_id  = "AllowHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = each.value.name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*/${each.value.path}"
}

moved {
  from = aws_apigatewayv2_integration.scoring
  to   = aws_apigatewayv2_integration.function["scoring"]
}

moved {
  from = aws_apigatewayv2_route.score
  to   = aws_apigatewayv2_route.route["POST /score"]
}

moved {
  from = aws_lambda_permission.api
  to   = aws_lambda_permission.function["scoring"]
}
