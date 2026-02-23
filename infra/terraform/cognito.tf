############################################################
# AWS Cognito — User Pool + App Client
# Used as the OIDC issuer for JWT authentication
############################################################

resource "aws_cognito_user_pool" "main" {
  name = "${local.name_prefix}-user-pool"

  # ── Sign-in options ───────────────────────────────────────
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # ── Password policy ───────────────────────────────────────
  password_policy {
    minimum_length                   = 12
    require_uppercase                = true
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # ── MFA ───────────────────────────────────────────────────
  mfa_configuration = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  # ── Standard attributes ───────────────────────────────────
  schema {
    name                     = "email"
    attribute_data_type      = "String"
    developer_only_attribute = false
    mutable                  = true
    required                 = true
    string_attribute_constraints {
      min_length = 5
      max_length = 256
    }
  }

  # ── Custom attributes (tenant isolation) ─────────────────
  schema {
    name                     = "tenant_id"
    attribute_data_type      = "String"
    developer_only_attribute = false
    mutable                  = true
    required                 = false
    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  schema {
    name                     = "role"
    attribute_data_type      = "String"
    developer_only_attribute = false
    mutable                  = true
    required                 = false
    string_attribute_constraints {
      min_length = 1
      max_length = 16
    }
  }

  # ── Account recovery ─────────────────────────────────────
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # ── Token validity ────────────────────────────────────────
  user_pool_add_ons {
    advanced_security_mode = var.environment == "prod" ? "ENFORCED" : "OFF"
  }

  # ── Email configuration ───────────────────────────────────
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  # ── Verification message ──────────────────────────────────
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Your ${var.project_name} verification code"
    email_message        = "Your verification code is {####}"
  }

  tags = { Name = "${local.name_prefix}-user-pool" }
}

# ── App Client (used by the backend JWT validator) ────────────
resource "aws_cognito_user_pool_client" "api" {
  name         = "${local.name_prefix}-api-client"
  user_pool_id = aws_cognito_user_pool.main.id

  # No secret — frontend SPA can't keep secrets
  generate_secret = false

  # Token validity
  access_token_validity  = 60   # 1 hour (minutes)
  id_token_validity      = 60
  refresh_token_validity = 30   # 30 days (days)

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # OAuth flows
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_PASSWORD_AUTH",
  ]

  # Attribute read/write permissions
  read_attributes  = ["email", "name", "custom:tenant_id", "custom:role"]
  write_attributes = ["email", "name", "custom:tenant_id", "custom:role"]

  prevent_user_existence_errors = "ENABLED"
}

# ── Cognito Domain (for hosted UI — optional) ────────────────
resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name_prefix}-auth-${local.suffix}"
  user_pool_id = aws_cognito_user_pool.main.id
}
