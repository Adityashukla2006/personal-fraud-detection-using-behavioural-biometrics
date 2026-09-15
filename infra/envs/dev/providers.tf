# Credentials come from the environment (AWS_PROFILE), never from this file.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "bfd"
      ManagedBy   = "terraform"
      Environment = "dev"
    }
  }
}
