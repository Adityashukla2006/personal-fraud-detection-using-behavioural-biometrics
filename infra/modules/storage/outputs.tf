output "lake_bucket" {
  value = aws_s3_bucket.this["lake"].bucket
}

output "client_bucket" {
  value = aws_s3_bucket.this["client"].bucket
}

output "lake_key_arn" {
  value = aws_kms_key.lake.arn
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.client.domain_name
}

output "client_url" {
  value = "https://${aws_cloudfront_distribution.client.domain_name}"
}
