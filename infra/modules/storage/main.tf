# S3 buckets: the audit lake, under its own customer-managed key, and the static client bucket
# that CloudFront will front in Phase 4 (architecture sections 11 and 13).

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
  account_id   = data.aws_caller_identity.current.account_id
  account_root = "arn:aws:iam::${local.account_id}:root"

  # Account ID keeps global bucket names unique without a random suffix to track.
  buckets = {
    lake   = "${var.name_prefix}-audit-lake-${local.account_id}"
    client = "${var.name_prefix}-client-${local.account_id}"
  }

  content_types = {
    html = "text/html; charset=utf-8"
    mjs  = "text/javascript; charset=utf-8"
    css  = "text/css; charset=utf-8"
    svg  = "image/svg+xml"
  }

  # config.mjs is generated below from live outputs; a local copy is never uploaded over it.
  client_files = toset([
    for file in fileset(var.client_dir, "**") : file
    if file != "config.mjs" && contains(keys(local.content_types), try(regex("[^.]+$", file), ""))
  ])
}

# Same shape as the templates key, scoped to S3: distinct keys mean a grant on the lake never
# reaches behavioural templates.
data "aws_iam_policy_document" "lake_key" {
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
    sid = "UseOnlyThroughS3"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = [local.account_root]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${data.aws_region.current.region}.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_kms_key" "lake" {
  description             = "${var.name_prefix} audit lake objects"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.lake_key.json
}

resource "aws_kms_alias" "lake" {
  name          = "alias/${var.name_prefix}-lake"
  target_key_id = aws_kms_key.lake.key_id
}

resource "aws_s3_bucket" "this" {
  for_each = local.buckets

  bucket = each.value

  # The stack must be destroyable from empty. The lake holds only reproducible decision events.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = local.buckets

  bucket                  = aws_s3_bucket.this[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = local.buckets

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.this["lake"].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.lake.arn
    }

    # One data key per bucket rather than per object keeps KMS request charges negligible.
    bucket_key_enabled = true
  }
}

# Client assets are public web content served through CloudFront, so SSE-S3 is sufficient.
resource "aws_s3_bucket_server_side_encryption_configuration" "client" {
  bucket = aws_s3_bucket.this["client"].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.this["lake"].id

  rule {
    id     = "glacier-instant-retrieval-after-90-days"
    status = "Enabled"

    filter {}

    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
  }
}

data "aws_iam_policy_document" "tls_only" {
  for_each = local.buckets

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.this[each.key].arn,
      "${aws_s3_bucket.this[each.key].arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # The client bucket stays private; only this distribution may read it, via origin access control.
  dynamic "statement" {
    for_each = each.key == "client" ? [aws_cloudfront_distribution.client.arn] : []

    content {
      sid       = "AllowCloudFrontRead"
      actions   = ["s3:GetObject"]
      resources = ["${aws_s3_bucket.this[each.key].arn}/*"]

      principals {
        type        = "Service"
        identifiers = ["cloudfront.amazonaws.com"]
      }

      condition {
        test     = "StringEquals"
        variable = "AWS:SourceArn"
        values   = [statement.value]
      }
    }
  }
}

resource "aws_cloudfront_origin_access_control" "client" {
  name                              = "${var.name_prefix}-client"
  description                       = "CloudFront access to the private client bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}

resource "aws_cloudfront_distribution" "client" {
  enabled             = true
  comment             = "${var.name_prefix} banking client"
  default_root_object = "index.html"
  http_version        = "http2and3"
  # PriceClass_200 includes edge locations in India, where the stack and its users are.
  price_class = "PriceClass_200"

  origin {
    origin_id                = "client"
    domain_name              = aws_s3_bucket.this["client"].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.client.id
  }

  default_cache_behavior {
    target_origin_id       = "client"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # Caching disabled in dev: a redeploy is visible immediately without invalidations.
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

resource "aws_s3_object" "client" {
  for_each = local.client_files

  bucket       = aws_s3_bucket.this["client"].id
  key          = each.value
  source       = "${var.client_dir}/${each.value}"
  source_hash  = filemd5("${var.client_dir}/${each.value}")
  content_type = local.content_types[regex("[^.]+$", each.value)]
}

resource "aws_s3_object" "client_config" {
  bucket       = aws_s3_bucket.this["client"].id
  key          = "config.mjs"
  content      = "export default ${jsonencode(var.client_config)};\n"
  content_type = local.content_types["mjs"]
}

resource "aws_s3_bucket_policy" "this" {
  for_each = local.buckets

  bucket = aws_s3_bucket.this[each.key].id
  policy = data.aws_iam_policy_document.tls_only[each.key].json

  depends_on = [aws_s3_bucket_public_access_block.this]
}
