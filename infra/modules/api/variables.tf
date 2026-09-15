variable "name_prefix" {
  type = string
}

variable "allowed_origins" {
  description = "CORS origins; only the CloudFront client."
  type        = list(string)
}

variable "jwt_issuer" {
  type = string
}

variable "jwt_audience" {
  type = list(string)
}

variable "function_name" {
  type = string
}

variable "function_invoke_arn" {
  type = string
}

variable "throttling_burst_limit" {
  type    = number
  default = 20
}

variable "throttling_rate_limit" {
  type    = number
  default = 10
}
