variable "name_prefix" {
  type = string
}

variable "relying_party_id" {
  description = "WebAuthn relying party: the domain the client is served from."
  type        = string
}
