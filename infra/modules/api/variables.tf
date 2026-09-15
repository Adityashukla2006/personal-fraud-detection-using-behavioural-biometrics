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

variable "functions" {
  description = "Integrated functions: name, invoke ARN, and the path their invoke permission covers."
  type = map(object({
    name       = string
    invoke_arn = string
    path       = string
  }))
}

variable "routes" {
  description = "Route key to the functions entry that serves it."
  type        = map(string)
}

variable "throttling_burst_limit" {
  type    = number
  default = 20
}

variable "throttling_rate_limit" {
  type    = number
  default = 10
}
