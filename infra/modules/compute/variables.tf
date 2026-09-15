variable "name_prefix" {
  type = string
}

variable "table_arn" {
  type = string
}

variable "table_name" {
  type = string
}

variable "table_key_arn" {
  description = "Customer-managed KMS key encrypting the table."
  type        = string
}

variable "build_dir" {
  description = "Directory 'make build' stages function packages into."
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 14
}
