# One IAM role and one log group per Lambda function. The functions themselves arrive from Phase 4
# onward; their roles exist first so least privilege is designed rather than retrofitted.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

locals {
  # Partition-key prefixes each function may read and write. IAM can scope DynamoDB by partition
  # key (dynamodb:LeadingKeys) but not by sort key, so every rule here is a partition prefix.
  functions = {
    scoring = {
      read  = ["USER#*", "PAYEE#*", "SESS#*"]
      write = ["SESS#*", "DEC#*"]
    }
    # The only writer of profile and buffer items.
    adaptation = {
      read  = ["USER#*"]
      write = ["USER#*"]
    }
    # Writes USER#<uid> / AGG#<window> and PAYEE#<pid> / RISK. Sharing the USER# partition with
    # profiles means IAM alone cannot stop this role overwriting a profile item.
    aggregator = {
      read  = []
      write = ["USER#*", "PAYEE#*"]
    }
    ledger = {
      read  = ["LEDGER#*"]
      write = ["LEDGER#*"]
    }
  }
}

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "function" {
  for_each = local.functions

  name               = "${var.name_prefix}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_cloudwatch_log_group" "function" {
  for_each = local.functions

  name              = "/aws/lambda/${var.name_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "function" {
  for_each = local.functions

  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function[each.key].arn}:*"]
  }

  statement {
    sid       = "PublishTraces"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }

  dynamic "statement" {
    for_each = length(each.value.read) > 0 ? [each.value.read] : []

    content {
      sid       = "ReadOwnPrefixes"
      actions   = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query"]
      resources = [var.table_arn]

      condition {
        test     = "ForAllValues:StringLike"
        variable = "dynamodb:LeadingKeys"
        values   = statement.value
      }
    }
  }

  dynamic "statement" {
    for_each = length(each.value.write) > 0 ? [each.value.write] : []

    content {
      sid       = "WriteOwnPrefixes"
      actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
      resources = [var.table_arn]

      condition {
        test     = "ForAllValues:StringLike"
        variable = "dynamodb:LeadingKeys"
        values   = statement.value
      }
    }
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.functions

  name   = "${var.name_prefix}-${each.key}"
  role   = aws_iam_role.function[each.key].id
  policy = data.aws_iam_policy_document.function[each.key].json
}
