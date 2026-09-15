variable "name_prefix" {
  type = string
}

variable "client_dir" {
  description = "Static client source directory uploaded to the client bucket."
  type        = string
}

variable "client_config" {
  description = "Public runtime settings written to config.mjs: region, pool, client and API URL."
  type        = map(string)
}
