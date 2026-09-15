# The audit lake as a queryable table, and the Athena workgroup the aggregator runs in (architecture
# sections 5.4 and 11). Partition projection derives every partition from the key layout the archive
# writes, so there is no crawler to schedule and no partition to register.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

locals {
  # Glue and Athena names allow underscores but not hyphens.
  database = replace("${var.name_prefix}_lake", "-", "_")
}

resource "aws_glue_catalog_database" "lake" {
  name        = local.database
  description = "${var.name_prefix} audit lake"
}

resource "aws_glue_catalog_table" "events" {
  name          = "events"
  database_name = aws_glue_catalog_database.lake.name
  description   = "Every bfd event, one JSON object per file, partitioned by type and UTC date"
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    classification                 = "json"
    "projection.enabled"           = "true"
    "projection.event_type.type"   = "enum"
    "projection.event_type.values" = "decision.scored,stepup.verified,transfer.completed"
    "projection.dt.type"           = "date"
    "projection.dt.range"          = "2026-01-01,NOW"
    "projection.dt.format"         = "yyyy-MM-dd"
    "projection.dt.interval"       = "1"
    "projection.dt.interval.unit"  = "DAYS"
    "storage.location.template"    = "s3://${var.lake_bucket}/events/type=$${event_type}/dt=$${dt}/"
  }

  partition_keys {
    name = "event_type"
    type = "string"
  }

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.lake_bucket}/events/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "id"
      type = "string"
    }

    columns {
      name = "source"
      type = "string"
    }

    columns {
      name = "time"
      type = "string"
    }

    # Only the fields the batch layer reads. The JSON SerDe ignores the rest of each detail.
    columns {
      name = "detail"
      type = "struct<uid:string,decision_id:string,transfer_id:string,checkpoint:string,status:string,amount:double,payee_id:string,scores:struct<behaviour:struct<score:double,confidence:double>>>"
    }
  }
}

# Results go under their own prefix of the lake, under the lake's key, and expire (storage module).
# The scan cutoff caps what one runaway query can cost.
resource "aws_athena_workgroup" "batch" {
  name          = "${var.name_prefix}-batch"
  description   = "Nightly batch features over the audit lake"
  force_destroy = true

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = 1073741824

    result_configuration {
      output_location = "s3://${var.lake_bucket}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = var.lake_key_arn
      }
    }
  }
}
