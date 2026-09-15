# Research figures and tables, published beside the client so the operator console can show the
# committed results. Read-only copies: research/results in the repository stays the source of truth.

locals {
  research_files = toset(concat(
    tolist(fileset(var.research_dir, "figures/*.png")),
    tolist(fileset(var.research_dir, "tables/*.csv")),
  ))
}

resource "aws_s3_object" "research" {
  for_each = local.research_files

  bucket       = aws_s3_bucket.this["client"].id
  key          = "research/${each.value}"
  source       = "${var.research_dir}/${each.value}"
  source_hash  = filemd5("${var.research_dir}/${each.value}")
  content_type = local.content_types[regex("[^.]+$", each.value)]
}
