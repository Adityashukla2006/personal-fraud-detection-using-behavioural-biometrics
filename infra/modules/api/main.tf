# HTTP API in front of the scoring Lambda: JWT validation and throttling at the edge, so scoring
# never runs for unauthenticated or flooding traffic (architecture section 11.1).

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
    allow_methods = ["POST"]
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

resource "aws_apigatewayv2_integration" "scoring" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = var.function_invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 10000
}

resource "aws_apigatewayv2_route" "score" {
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = "POST /score"
  target             = "integrations/${aws_apigatewayv2_integration.scoring.id}"
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

# The invoke permission belongs to the API because the API is the caller it authorises.
resource "aws_lambda_permission" "api" {
  statement_id  = "AllowHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*/score"
}
