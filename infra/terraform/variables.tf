############################################################
# Input Variables
############################################################

variable "project_name" {
  description = "Short project identifier used as a prefix for all resources"
  type        = string
  default     = "rag-platform"
}

variable "environment" {
  description = "Deployment environment (prod | staging | dev)"
  type        = string
  default     = "prod"
  validation {
    condition     = contains(["prod", "staging", "dev"], var.environment)
    error_message = "environment must be prod, staging, or dev"
  }
}

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

# ── Networking ──────────────────────────────────────────────
variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "AZs to use (minimum 2 for HA)"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

# ── Domain & TLS ────────────────────────────────────────────
variable "domain_name" {
  description = "Root domain (e.g. ragplatform.io). Leave empty to use ALB DNS name."
  type        = string
  default     = ""
}

variable "api_subdomain" {
  description = "Subdomain for the API"
  type        = string
  default     = "api"
}

variable "app_subdomain" {
  description = "Subdomain for the frontend app"
  type        = string
  default     = "app"
}

# ── Database ────────────────────────────────────────────────
variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.medium"
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "ragplatform"
}

variable "db_username" {
  description = "PostgreSQL master username"
  type        = string
  default     = "rag_admin"
}

variable "db_allocated_storage" {
  description = "RDS storage in GB"
  type        = number
  default     = 50
}

variable "db_multi_az" {
  description = "Enable Multi-AZ for RDS (recommended for prod)"
  type        = bool
  default     = true
}

# ── Cache ───────────────────────────────────────────────────
variable "redis_node_type" {
  description = "ElastiCache Redis node type"
  type        = string
  default     = "cache.t3.micro"
}

# ── Vector Store ─────────────────────────────────────────────
variable "weaviate_url" {
  description = "Weaviate Cloud URL (e.g. https://xxx.weaviate.network)"
  type        = string
}

variable "weaviate_api_key" {
  description = "Weaviate Cloud API key (stored in Secrets Manager)"
  type        = string
  sensitive   = true
}

# ── AI / LLM ────────────────────────────────────────────────
variable "openai_api_key" {
  description = "OpenAI API key"
  type        = string
  sensitive   = true
}

variable "cohere_api_key" {
  description = "Cohere API key (for ReRank)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "langsmith_api_key" {
  description = "LangSmith API key (optional — for LLM tracing)"
  type        = string
  sensitive   = true
  default     = ""
}

# ── ECS Task Sizing ──────────────────────────────────────────
variable "api_cpu" {
  description = "API task CPU (256=0.25vCPU, 1024=1vCPU)"
  type        = number
  default     = 1024
}

variable "api_memory" {
  description = "API task memory in MB"
  type        = number
  default     = 2048
}

variable "api_desired_count" {
  description = "Desired number of API tasks"
  type        = number
  default     = 2
}

variable "worker_cpu" {
  description = "Celery worker task CPU"
  type        = number
  default     = 1024
}

variable "worker_memory" {
  description = "Celery worker task memory in MB"
  type        = number
  default     = 2048
}

variable "worker_desired_count" {
  description = "Desired number of Celery worker tasks"
  type        = number
  default     = 1
}

variable "frontend_cpu" {
  description = "Frontend task CPU"
  type        = number
  default     = 512
}

variable "frontend_memory" {
  description = "Frontend task memory in MB"
  type        = number
  default     = 1024
}
