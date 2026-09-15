variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "budget_limit_usd" {
  description = "Monthly cost budget. Alerts fire at 80% actual, 100% forecast and 100% actual."
  type        = number
  default     = 5
}

variable "alert_email_enabled" {
  description = "Deliver restrict and block alerts by email. Turn off during load runs."
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Address that receives budget alerts. Set in terraform.tfvars, which is not committed."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}
