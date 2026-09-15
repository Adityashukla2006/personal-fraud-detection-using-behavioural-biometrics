variable "name_prefix" {
  type = string
}

variable "client_dir" {
  description = "Static client source directory uploaded to the client bucket."
  type        = string
}

variable "research_dir" {
  description = "research/results, whose figures and tables are published for the console."
  type        = string
}

variable "client_config" {
  description = "Public runtime settings written to config.mjs: region, pool, client and API URL."
  type        = map(string)
}
