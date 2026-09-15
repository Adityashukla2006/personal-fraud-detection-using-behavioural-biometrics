variable "name_prefix" {
  type = string
}

variable "table_arn" {
  type = string
}

variable "log_retention_days" {
  type    = number
  default = 14
}
