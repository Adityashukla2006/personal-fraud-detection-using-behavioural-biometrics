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

variable "state_machine_arn" {
  description = "Transfer response workflow that scoring starts."
  type        = string
}

variable "cognito_client_id" {
  description = "App client the transfers function uses to run passkey step-up."
  type        = string
}

variable "step_up_timeout_seconds" {
  description = "How long a transfer waits for passkey step-up before it is cancelled."
  type        = number
  default     = 300
}

variable "review_timeout_seconds" {
  description = "How long a restricted transfer waits for analyst review before it is cancelled."
  type        = number
  default     = 3600
}

variable "build_dir" {
  description = "Directory 'make build' stages function packages into."
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 14
}
