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
