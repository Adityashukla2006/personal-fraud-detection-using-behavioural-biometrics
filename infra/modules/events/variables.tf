variable "name_prefix" {
  type = string
}

variable "archive_function_arn" {
  type = string
}

variable "archive_function_name" {
  type = string
}

variable "adaptation_function_arn" {
  type = string
}

variable "adaptation_function_name" {
  type = string
}

variable "aggregator_function_arn" {
  type = string
}

variable "aggregator_schedule" {
  description = "When the batch aggregator runs, in Asia/Kolkata time."
  type        = string
  default     = "cron(0 2 * * ? *)"
}
