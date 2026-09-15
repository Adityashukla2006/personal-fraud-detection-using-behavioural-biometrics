# The response plane: one Step Functions Standard execution per confirmed transfer (architecture
# section 3). Standard rather than Express because a step-up or a review can outlast Express's
# five-minute limit, and the platform owns the wait instead of a Lambda paying for it.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

data "aws_region" "current" {}

locals {
  # Transient Lambda service errors only. A ledger error that is not transient fails the execution,
  # which leaves the transfer undebited.
  lambda_retry = jsonencode([
    {
      ErrorEquals = [
        "Lambda.ServiceException",
        "Lambda.AWSLambdaException",
        "Lambda.SdkClientException",
        "Lambda.TooManyRequestsException",
      ]
      IntervalSeconds = 1
      MaxAttempts     = 3
      BackoffRate     = 2
    }
  ])
}

data "aws_iam_policy_document" "assume_states" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "workflow" {
  name               = "${var.name_prefix}-transfer-workflow"
  assume_role_policy = data.aws_iam_policy_document.assume_states.json
}

data "aws_iam_policy_document" "workflow" {
  statement {
    sid       = "InvokeLedger"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.ledger_function_arn, "${var.ledger_function_arn}:*"]
  }

  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = [var.alerts_topic_arn]
  }

  # The alerts topic is encrypted, so publishing needs a data key, obtainable only through SNS.
  statement {
    sid       = "EncryptAlertsThroughSns"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["sns.${data.aws_region.current.region}.amazonaws.com"]
    }
  }

  statement {
    sid = "PublishTraces"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "workflow" {
  name   = "${var.name_prefix}-transfer-workflow"
  role   = aws_iam_role.workflow.id
  policy = data.aws_iam_policy_document.workflow.json
}

resource "aws_sfn_state_machine" "transfer" {
  name     = "${var.name_prefix}-transfer"
  type     = "STANDARD"
  role_arn = aws_iam_role.workflow.arn

  definition = templatefile("${path.module}/transfer.asl.json.tftpl", {
    ledger_arn   = var.ledger_function_arn
    topic_arn    = var.alerts_topic_arn
    lambda_retry = local.lambda_retry
  })

  tracing_configuration {
    enabled = true
  }

  depends_on = [aws_iam_role_policy.workflow]
}
