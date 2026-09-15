# One IAM role and one log group per Lambda function, and the functions deployed so far. Roles
# exist before their functions so least privilege is designed rather than retrofitted.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    archive = {
      source = "hashicorp/archive"
    }
  }
}

locals {
  # Partition-key prefixes each function may read and write. IAM can scope DynamoDB by partition
  # key (dynamodb:LeadingKeys) but not by sort key, so every rule here is a partition prefix.
  functions = {
    scoring = {
      read  = ["USER#*", "PAYEE#*", "AGG#*", "SESS#*"]
      write = ["SESS#*", "DEC#*"]
    }
    # The only writer of profile and buffer items.
    adaptation = {
      read  = ["USER#*"]
      write = ["USER#*"]
    }
    # Writes AGG#<uid> / WINDOW#<window> and PAYEE#<pid> / RISK. Aggregates have their own
    # partition precisely so this role never needs, and never gets, write access to USER#.
    aggregator = {
      read  = []
      write = ["AGG#*", "PAYEE#*"]
    }
    ledger = {
      read  = ["LEDGER#*"]
      write = ["LEDGER#*"]
    }
  }
}

data "aws_region" "current" {}

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

  # The table is encrypted under a customer-managed key, so every caller needs KMS permissions of its
  # own; DynamoDB does not use them on the caller's behalf. Scoped to the one key, and only when
  # DynamoDB is the one asking, so these roles still cannot decrypt anything directly.
  dynamic "statement" {
    for_each = length(each.value.read) + length(each.value.write) > 0 ? [1] : []

    content {
      sid       = "UseTableKeyThroughDynamoDB"
      actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
      resources = [var.table_key_arn]

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["dynamodb.${data.aws_region.current.region}.amazonaws.com"]
      }
    }
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

# The package is staged by 'make build' and only zipped here, so Terraform never runs a build.
data "archive_file" "scoring" {
  type        = "zip"
  source_dir  = "${var.build_dir}/scoring"
  output_path = "${var.build_dir}/scoring.zip"
}

resource "aws_lambda_function" "scoring" {
  function_name    = "${var.name_prefix}-scoring"
  role             = aws_iam_role.function["scoring"].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "scoring.handler.lambda_handler"
  memory_size      = 512
  timeout          = 5
  filename         = data.archive_file.scoring.output_path
  source_code_hash = data.archive_file.scoring.output_base64sha256

  environment {
    variables = {
      TABLE_NAME = var.table_name
    }
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.function["scoring"].name
  }

  depends_on = [aws_iam_role_policy.function]
}
