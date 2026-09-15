variable "name_prefix" {
  type = string
}

variable "budget_limit_usd" {
  type = number
}

variable "alert_email" {
  type = string
}

variable "alert_email_enabled" {
  type    = bool
  default = true
}

variable "xray_sampling_rate" {
  type    = number
  default = 1.0
}
