variable "name_prefix" {
  type = string
}

variable "lake_bucket" {
  description = "Audit lake bucket the events table reads and query results are written to."
  type        = string
}

variable "lake_key_arn" {
  description = "Customer-managed KMS key encrypting the lake and query results."
  type        = string
}
