# Cognito user pool with passkey sign-in (architecture section 11.1). The device-bound passkey is
# what the trust model rests on: it gates step-up now and profile adaptation later.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

resource "aws_cognito_user_pool" "main" {
  name = "${var.name_prefix}-users"

  # Passkeys as a first factor need the Essentials tier. Its free tier covers 10,000 monthly active
  # users, far beyond this project.
  user_pool_tier      = "ESSENTIALS"
  deletion_protection = "INACTIVE"
  mfa_configuration   = "OFF"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # The client is served from a public URL. Admin-created accounts only, so nobody can sign up and
  # generate traffic against the budget.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  sign_in_policy {
    allowed_first_auth_factors = ["PASSWORD", "WEB_AUTHN"]
  }

  web_authn_configuration {
    relying_party_id  = var.relying_party_id
    user_verification = "required"
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

# Operators who may see every user's decisions and release transfers held for review.
resource "aws_cognito_user_group" "analyst" {
  name         = "analyst"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Operator console access: cross-user view and review release"
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.name_prefix}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  # A browser cannot keep a secret.
  generate_secret = false

  # USER_AUTH is choice-based sign-in: password or passkey, chosen per attempt.
  explicit_auth_flows = ["ALLOW_USER_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  auth_session_validity         = 3

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
