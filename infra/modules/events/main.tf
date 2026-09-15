# The event bus and its rules (architecture section 5.3). Adding a consumer here never touches the
# scoring function, which is the point of routing through a bus rather than invoking directly.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

resource "aws_cloudwatch_event_bus" "main" {
  name = "${var.name_prefix}-events"
}

# Everything this system publishes goes to the audit lake.
resource "aws_cloudwatch_event_rule" "archive" {
  name           = "${var.name_prefix}-archive-all"
  description    = "Every bfd event to the S3 audit lake"
  event_bus_name = aws_cloudwatch_event_bus.main.name
  event_pattern  = jsonencode({ source = [{ prefix = "bfd." }] })
}

resource "aws_cloudwatch_event_target" "archive" {
  rule           = aws_cloudwatch_event_rule.archive.name
  event_bus_name = aws_cloudwatch_event_bus.main.name
  arn            = var.archive_function_arn

  # An audit record is worth a day of retries if the archive function is throttled.
  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 24
  }
}

# Profile learning only from passkey-verified step-ups. The pattern matches on the verification
# method, so a transfer released any other way never reaches adaptation.
resource "aws_cloudwatch_event_rule" "stepup_verified" {
  name           = "${var.name_prefix}-stepup-verified"
  description    = "Passkey-verified step-ups to the adaptation function"
  event_bus_name = aws_cloudwatch_event_bus.main.name

  event_pattern = jsonencode({
    source        = ["bfd.workflow"]
    "detail-type" = ["stepup.verified"]
    detail = {
      verification = {
        method = ["passkey"]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "stepup_verified" {
  rule           = aws_cloudwatch_event_rule.stepup_verified.name
  event_bus_name = aws_cloudwatch_event_bus.main.name
  arn            = var.adaptation_function_arn

  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 24
  }
}

# Invoke permissions belong to the bus rules, the callers they authorise.
resource "aws_lambda_permission" "archive" {
  statement_id  = "AllowEventBridgeArchive"
  action        = "lambda:InvokeFunction"
  function_name = var.archive_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.archive.arn
}

resource "aws_lambda_permission" "stepup_verified" {
  statement_id  = "AllowEventBridgeStepUpVerified"
  action        = "lambda:InvokeFunction"
  function_name = var.adaptation_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.stepup_verified.arn
}
