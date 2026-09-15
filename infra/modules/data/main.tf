# The single DynamoDB table and the customer-managed key over behavioural templates
# (architecture sections 6 and 13). The key lives here because the table is the only thing it
# encrypts; the audit lake has its own key in the storage module.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_root = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
}

# Restrictive by design: account principals may administer the key, but may only use it through
# DynamoDB. Nobody can call kms:Decrypt on it directly, so templates cannot be read around the table.
data "aws_iam_policy_document" "templates_key" {
  statement {
    sid = "KeyAdministration"
    actions = [
      "kms:Create*",
      "kms:Describe*",
      "kms:Enable*",
      "kms:List*",
      "kms:Put*",
      "kms:Update*",
      "kms:Revoke*",
      "kms:Disable*",
      "kms:Get*",
      "kms:Delete*",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:ScheduleKeyDeletion",
      "kms:CancelKeyDeletion",
    ]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = [local.account_root]
    }
  }

  statement {
    sid = "UseOnlyThroughDynamoDB"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = [local.account_root]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${data.aws_region.current.region}.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_kms_key" "templates" {
  description             = "${var.name_prefix} behavioural templates and decisions in DynamoDB"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.templates_key.json
}

resource "aws_kms_alias" "templates" {
  name          = "alias/${var.name_prefix}-templates"
  target_key_id = aws_kms_key.templates.key_id
}

resource "aws_dynamodb_table" "main" {
  name         = "${var.name_prefix}-main"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  # Chronological per-user decisions, for evaluation and demos.
  global_secondary_index {
    name            = "GSI1"
    projection_type = "ALL"

    key_schema {
      attribute_name = "GSI1PK"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "GSI1SK"
      key_type       = "RANGE"
    }
  }

  # Sessions 1 day, decisions 30 days, buffer 180 days: each item carries its own expiry.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.templates.arn
  }

  # Continuous backups bill per GB-month. Everything here is either reproducible or expires, and the
  # stack must be destroyable from empty, so neither backups nor deletion protection are enabled.
  point_in_time_recovery {
    enabled = false
  }

  deletion_protection_enabled = false
}
