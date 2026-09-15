# Cost guardrail for the whole account. Later phases add dashboards, alarms and X-Ray here.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-monthly-cost"
  budget_type  = "COST"
  limit_amount = format("%.1f", var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Early warning while there is still room to stop a runaway simulator loop.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}

# Analyst alerts for restricted and blocked transfers. Encrypted with the AWS-managed SNS key: the
# messages carry identifiers only, no amounts or account numbers.
resource "aws_sns_topic" "alerts" {
  name              = "${var.name_prefix}-alerts"
  kms_master_key_id = "alias/aws/sns"
}

# Email subscriptions must be confirmed from the inbox before alerts are delivered. The switch lets
# a load run pause delivery without touching the workflow: alerts are still published and measured,
# they just reach no inbox. Re-enabling sends a fresh confirmation email.
resource "aws_sns_topic_subscription" "alerts_email" {
  count = var.alert_email_enabled ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

moved {
  from = aws_sns_topic_subscription.alerts_email
  to   = aws_sns_topic_subscription.alerts_email[0]
}

# Latency is a reported result, so dev traces every request by default rather than the 5% default
# sampling rate. Traces are free well beyond the traffic this project generates.
resource "aws_xray_sampling_rule" "functions" {
  rule_name      = "${var.name_prefix}-functions"
  priority       = 1000
  version        = 1
  reservoir_size = 1
  fixed_rate     = var.xray_sampling_rate
  service_name   = "${var.name_prefix}-*"
  service_type   = "*"
  host           = "*"
  http_method    = "*"
  url_path       = "*"
  resource_arn   = "*"
}
