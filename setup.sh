#!/usr/bin/env bash
# =============================================================================
#  RAG Platform — One-Time AWS Setup Script
#  Run this ONCE. After it finishes, every git push auto-deploys.
# =============================================================================
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════${NC}"; \
            echo -e "${BOLD}${CYAN}  $*${NC}"; \
            echo -e "${BOLD}${CYAN}══════════════════════════════════════════${NC}"; }
ask()     { echo -e "${YELLOW}$*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# =============================================================================
# STEP 0 — Banner
# =============================================================================
echo ""
echo -e "${BOLD}${CYAN}"
echo "  ██████╗  █████╗  ██████╗     ██████╗ ██╗      █████╗ ████████╗"
echo "  ██╔══██╗██╔══██╗██╔════╝     ██╔══██╗██║     ██╔══██╗╚══██╔══╝"
echo "  ██████╔╝███████║██║  ███╗    ██████╔╝██║     ███████║   ██║   "
echo "  ██╔══██╗██╔══██║██║   ██║    ██╔═══╝ ██║     ██╔══██║   ██║   "
echo "  ██║  ██║██║  ██║╚██████╔╝    ██║     ███████╗██║  ██║   ██║   "
echo "  ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝     ╚═╝     ╚══════╝╚═╝  ╚═╝   ╚═╝   "
echo -e "${NC}"
echo -e "${BOLD}  AWS One-Time Setup Script${NC}"
echo ""
echo "  This script will:"
echo "   1. Check your tools are installed"
echo "   2. Ask you 6 questions"
echo "   3. Create all AWS infrastructure automatically"
echo "   4. Build and deploy your application"
echo "   5. Print GitHub secrets to paste (for auto-deploy)"
echo ""
echo -e "  ${YELLOW}Total time: ~25 minutes${NC}"
echo ""
read -p "  Press ENTER to start, or Ctrl+C to cancel..."

# =============================================================================
# STEP 1 — Check required tools
# =============================================================================
step "STEP 1 of 8 — Checking required tools"

check_tool() {
  if command -v "$1" &>/dev/null; then
    success "$1 is installed ($(command -v $1))"
  else
    error "$1 is NOT installed. Please install it first.

    Install instructions:
    - AWS CLI:   https://aws.amazon.com/cli/
    - Terraform: https://developer.hashicorp.com/terraform/install
    - Docker:    https://docs.docker.com/get-docker/
    "
  fi
}

check_tool aws
check_tool terraform
check_tool docker

# Check Docker is running
if ! docker info &>/dev/null; then
  error "Docker is installed but NOT running. Please start Docker Desktop and try again."
fi
success "Docker is running"

# Check AWS credentials
if ! aws sts get-caller-identity &>/dev/null; then
  error "AWS CLI is not configured. Run: aws configure

  You need:
    - AWS Access Key ID
    - AWS Secret Access Key

  Get these from: AWS Console → IAM → Users → your user → Security credentials → Create access key"
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CURRENT_USER=$(aws sts get-caller-identity --query Arn --output text)
success "AWS connected as: $CURRENT_USER"

# =============================================================================
# STEP 2 — Collect configuration
# =============================================================================
step "STEP 2 of 8 — Configuration (answer 6 questions)"

echo ""
echo "  Answer the questions below. Press ENTER to accept the [default]."
echo ""

# AWS Region
ask "  1. AWS Region [us-east-1]:"
read -r AWS_REGION
AWS_REGION="${AWS_REGION:-us-east-1}"
success "Region: $AWS_REGION"

# Project name
ask "  2. Project name [rag-platform]:"
read -r PROJECT_NAME
PROJECT_NAME="${PROJECT_NAME:-rag-platform}"
PROJECT_NAME="${PROJECT_NAME// /-}"   # replace spaces with hyphens
success "Project: $PROJECT_NAME"

# OpenAI key
echo ""
ask "  3. OpenAI API Key (starts with sk-):"
ask "     Get it from: https://platform.openai.com/api-keys"
read -r OPENAI_API_KEY
[[ -z "$OPENAI_API_KEY" ]] && error "OpenAI API key is required"
[[ "$OPENAI_API_KEY" != sk-* ]] && warn "Key doesn't start with sk- — double-check it"
success "OpenAI key: ${OPENAI_API_KEY:0:10}..."

# Weaviate
echo ""
ask "  4. Weaviate Cloud URL:"
ask "     Sign up free at https://console.weaviate.cloud → Create Cluster → Copy URL"
ask "     Example: https://abc123.weaviate.network"
read -r WEAVIATE_URL
[[ -z "$WEAVIATE_URL" ]] && error "Weaviate URL is required"
success "Weaviate URL: $WEAVIATE_URL"

echo ""
ask "     Weaviate API Key (from the cluster's API Keys tab):"
read -r WEAVIATE_API_KEY
[[ -z "$WEAVIATE_API_KEY" ]] && error "Weaviate API key is required"
success "Weaviate key: ${WEAVIATE_API_KEY:0:10}..."

# Cohere (optional)
echo ""
ask "  5. Cohere API Key (optional — for better search):"
ask "     Leave empty to skip. Get it from: https://dashboard.cohere.com/api-keys"
read -r COHERE_API_KEY
COHERE_API_KEY="${COHERE_API_KEY:-}"
if [[ -n "$COHERE_API_KEY" ]]; then
  success "Cohere key: ${COHERE_API_KEY:0:10}..."
else
  info "Cohere skipped (hybrid search will still work without ReRank)"
fi

# Admin email
echo ""
ask "  6. Admin user email (you will use this to log into the app):"
read -r ADMIN_EMAIL
[[ -z "$ADMIN_EMAIL" ]] && error "Admin email is required"
[[ "$ADMIN_EMAIL" != *@* ]] && error "Please enter a valid email address"
success "Admin email: $ADMIN_EMAIL"

echo ""
ask "     Admin password (min 12 chars, must include uppercase, lowercase, number):"
read -rs ADMIN_PASSWORD
echo ""
[[ ${#ADMIN_PASSWORD} -lt 12 ]] && error "Password must be at least 12 characters"
success "Admin password set"

# =============================================================================
# STEP 3 — Create Terraform state backend
# =============================================================================
step "STEP 3 of 8 — Creating Terraform state storage"

STATE_BUCKET="${PROJECT_NAME}-tfstate-${ACCOUNT_ID}"
LOCK_TABLE="${PROJECT_NAME}-tfstate-lock"

info "Creating S3 bucket for Terraform state: $STATE_BUCKET"

if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  success "State bucket already exists — skipping"
else
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket \
      --bucket "$STATE_BUCKET" \
      --region "$AWS_REGION" \
      --output text > /dev/null
  else
    aws s3api create-bucket \
      --bucket "$STATE_BUCKET" \
      --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" \
      --output text > /dev/null
  fi
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
    --versioning-configuration Status=Enabled > /dev/null
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true > /dev/null
  aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' > /dev/null
  success "State bucket created"
fi

info "Creating DynamoDB lock table: $LOCK_TABLE"

if aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" &>/dev/null; then
  success "Lock table already exists — skipping"
else
  aws dynamodb create-table \
    --table-name "$LOCK_TABLE" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$AWS_REGION" \
    --output text > /dev/null
  aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$AWS_REGION"
  success "Lock table created"
fi

# =============================================================================
# STEP 4 — Generate Terraform config files
# =============================================================================
step "STEP 4 of 8 — Generating Terraform configuration"

TERRAFORM_DIR="$SCRIPT_DIR/infra/terraform"

# Update backend config in main.tf
info "Updating Terraform state backend config..."
sed -i.bak \
  -e "s|bucket.*=.*\"rag-platform-tfstate\"|bucket         = \"$STATE_BUCKET\"|g" \
  -e "s|region.*=.*\"us-east-1\".*# backend|region         = \"$AWS_REGION\" # backend|g" \
  -e "s|dynamodb_table.*=.*\"rag-platform-tfstate-lock\"|dynamodb_table = \"$LOCK_TABLE\"|g" \
  "$TERRAFORM_DIR/main.tf" 2>/dev/null || true

# Generate terraform.tfvars
info "Writing terraform.tfvars..."
cat > "$TERRAFORM_DIR/terraform.tfvars" << EOF
# Auto-generated by setup.sh on $(date)
project_name = "$PROJECT_NAME"
environment  = "prod"
aws_region   = "$AWS_REGION"

vpc_cidr           = "10.0.0.0/16"
availability_zones = ["${AWS_REGION}a", "${AWS_REGION}b"]

domain_name   = ""
api_subdomain = "api"
app_subdomain = "app"

db_instance_class    = "db.t3.medium"
db_name              = "ragplatform"
db_username          = "rag_admin"
db_allocated_storage = 50
db_multi_az          = true

redis_node_type = "cache.t3.micro"

weaviate_url     = "$WEAVIATE_URL"
weaviate_api_key = "$WEAVIATE_API_KEY"

openai_api_key    = "$OPENAI_API_KEY"
cohere_api_key    = "${COHERE_API_KEY}"
langsmith_api_key = ""

api_cpu              = 1024
api_memory           = 2048
api_desired_count    = 2
worker_cpu           = 1024
worker_memory        = 2048
worker_desired_count = 1
frontend_cpu         = 512
frontend_memory      = 1024
EOF
success "terraform.tfvars written"

# =============================================================================
# STEP 5 — Run Terraform
# =============================================================================
step "STEP 5 of 8 — Creating AWS infrastructure (this takes ~15 minutes)"

cd "$TERRAFORM_DIR"

info "Running terraform init..."
terraform init \
  -backend-config="bucket=$STATE_BUCKET" \
  -backend-config="key=prod/terraform.tfstate" \
  -backend-config="region=$AWS_REGION" \
  -backend-config="dynamodb_table=$LOCK_TABLE" \
  -reconfigure \
  -input=false 2>&1 | tail -5

success "Terraform initialized"

info "Running terraform apply (creating all AWS resources)..."
info "This will create VPC, RDS, Redis, ECS, Cognito, S3, ALB, ECR, etc."
echo ""

terraform apply \
  -auto-approve \
  -input=false \
  2>&1 | grep -E "^\s*(aws_|module|Apply|Plan|Error|Error:)" | \
  while IFS= read -r line; do
    if [[ "$line" == *"Error"* ]]; then
      echo -e "${RED}  $line${NC}"
    else
      echo -e "${CYAN}  $line${NC}"
    fi
  done

echo ""
success "AWS infrastructure created!"

# Capture outputs
ALB_URL=$(terraform output -raw alb_dns_name 2>/dev/null)
ECR_API_URL=$(terraform output -raw ecr_api_url 2>/dev/null)
ECR_FRONTEND_URL=$(terraform output -raw ecr_frontend_url 2>/dev/null)
ECS_CLUSTER=$(terraform output -raw ecs_cluster_name 2>/dev/null)
ECS_API_SERVICE=$(terraform output -raw ecs_api_service_name 2>/dev/null)
ECS_WORKER_SERVICE=$(terraform output -raw ecs_worker_service_name 2>/dev/null)
ECS_FRONTEND_SERVICE=$(terraform output -raw ecs_frontend_service_name 2>/dev/null)
ECS_MIGRATION_TASK=$(terraform output -raw ecs_migration_task_definition 2>/dev/null)
COGNITO_POOL_ID=$(terraform output -raw cognito_user_pool_id 2>/dev/null)
COGNITO_CLIENT_ID=$(terraform output -raw cognito_client_id 2>/dev/null)

# Get private subnet and API security group for migration task
PRIVATE_SUBNET=$(terraform state show 'aws_subnet.private[0]' 2>/dev/null | grep '"id"' | head -1 | awk '{print $3}' | tr -d '"')
API_SG=$(terraform state show aws_security_group.ecs_api 2>/dev/null | grep '"id"' | head -1 | awk '{print $3}' | tr -d '"')

# Save outputs to file
terraform output > "$SCRIPT_DIR/terraform-outputs.txt" 2>/dev/null
success "Outputs saved to terraform-outputs.txt"

cd "$SCRIPT_DIR"

# =============================================================================
# STEP 6 — Build and push Docker images
# =============================================================================
step "STEP 6 of 8 — Building and pushing Docker images (~8 minutes)"

info "Logging in to AWS ECR..."
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS \
               --password-stdin \
               "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com" 2>&1 | tail -1
success "Logged in to ECR"

IMAGE_TAG="v1.0.0"

info "Building API image (this takes 3-6 minutes)..."
docker build \
  --target api \
  --tag "$ECR_API_URL:$IMAGE_TAG" \
  --tag "$ECR_API_URL:latest" \
  --progress=plain \
  ./backend 2>&1 | grep -E "^(Step|#[0-9]|Successfully|ERROR)" | tail -20

success "API image built"

info "Pushing API image to ECR..."
docker push "$ECR_API_URL:$IMAGE_TAG" --quiet
docker push "$ECR_API_URL:latest" --quiet
success "API image pushed"

info "Building frontend image (this takes 2-4 minutes)..."
docker build \
  --build-arg NEXT_PUBLIC_APP_VERSION="$IMAGE_TAG" \
  --tag "$ECR_FRONTEND_URL:$IMAGE_TAG" \
  --tag "$ECR_FRONTEND_URL:latest" \
  --progress=plain \
  ./frontend 2>&1 | grep -E "^(Step|#[0-9]|Successfully|ERROR)" | tail -20

success "Frontend image built"

info "Pushing frontend image to ECR..."
docker push "$ECR_FRONTEND_URL:$IMAGE_TAG" --quiet
docker push "$ECR_FRONTEND_URL:latest" --quiet
success "Frontend image pushed"

# =============================================================================
# STEP 7 — Run database migrations
# =============================================================================
step "STEP 7 of 8 — Running database migrations"

info "Starting migration task in ECS..."

TASK_ARN=$(aws ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$ECS_MIGRATION_TASK" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$PRIVATE_SUBNET],securityGroups=[$API_SG],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' \
  --output text)

info "Migration task started: $TASK_ARN"
info "Waiting for migrations to finish (1-3 minutes)..."

aws ecs wait tasks-stopped \
  --cluster "$ECS_CLUSTER" \
  --tasks "$TASK_ARN"

EXIT_CODE=$(aws ecs describe-tasks \
  --cluster "$ECS_CLUSTER" \
  --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' \
  --output text)

if [[ "$EXIT_CODE" == "0" ]]; then
  success "Database migrations completed"
else
  warn "Migration task exited with code $EXIT_CODE"
  warn "Check logs at: AWS Console → CloudWatch → Log groups → /ecs/${PROJECT_NAME}-prod/migration"
  warn "Continuing anyway — app may still work if tables already exist"
fi

# Update all ECS services
info "Deploying API service..."
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_API_SERVICE" \
  --force-new-deployment \
  --output text --query 'service.serviceName' > /dev/null

info "Deploying worker service..."
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_WORKER_SERVICE" \
  --force-new-deployment \
  --output text --query 'service.serviceName' > /dev/null

info "Deploying frontend service..."
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_FRONTEND_SERVICE" \
  --force-new-deployment \
  --output text --query 'service.serviceName' > /dev/null

success "All services deploying (takes 3-5 minutes to become healthy)"

# =============================================================================
# STEP 8 — Create admin user
# =============================================================================
step "STEP 8 of 8 — Creating your admin user"

info "Creating admin user in Cognito: $ADMIN_EMAIL"

aws cognito-idp admin-create-user \
  --user-pool-id "$COGNITO_POOL_ID" \
  --username "$ADMIN_EMAIL" \
  --user-attributes \
    Name=email,Value="$ADMIN_EMAIL" \
    Name=email_verified,Value=true \
    "Name=custom:role,Value=admin" \
    "Name=custom:tenant_id,Value=tenant_001" \
  --temporary-password "TempPass123!" \
  --message-action SUPPRESS \
  --output text > /dev/null 2>&1 || warn "User may already exist — continuing"

aws cognito-idp admin-set-user-password \
  --user-pool-id "$COGNITO_POOL_ID" \
  --username "$ADMIN_EMAIL" \
  --password "$ADMIN_PASSWORD" \
  --permanent \
  --output text > /dev/null

success "Admin user created"

# =============================================================================
# DONE — Print summary
# =============================================================================
echo ""
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║          ✅  SETUP COMPLETE!                              ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}  Your App URL:${NC}"
echo -e "  ${CYAN}https://${ALB_URL#https://}${NC}"
echo ""
echo -e "${BOLD}  Login credentials:${NC}"
echo -e "  Email:    ${CYAN}$ADMIN_EMAIL${NC}"
echo -e "  Password: ${CYAN}$ADMIN_PASSWORD${NC}"
echo ""
echo -e "${BOLD}  API docs:${NC}"
echo -e "  ${CYAN}https://${ALB_URL#https://}/api/docs${NC}"
echo ""
echo -e "  ${YELLOW}⚠️  Wait 3-5 more minutes for ECS containers to become healthy.${NC}"
echo ""
echo ""

# =============================================================================
# Print GitHub Secrets
# =============================================================================
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║   📋  GITHUB SECRETS — Copy these to your repo          ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Go to: ${CYAN}GitHub repo → Settings → Secrets and variables → Actions${NC}"
echo -e "  Click ${BOLD}'New repository secret'${NC} for each one below:"
echo ""
echo -e "${BOLD}  ─── SECRETS (sensitive) ─────────────────────────────────${NC}"
echo -e "  ${YELLOW}Name:${NC}  AWS_ACCESS_KEY_ID"
echo -e "  ${YELLOW}Value:${NC} $(aws configure get aws_access_key_id)"
echo ""
echo -e "  ${YELLOW}Name:${NC}  AWS_SECRET_ACCESS_KEY"
echo -e "  ${YELLOW}Value:${NC} $(aws configure get aws_secret_access_key)"
echo ""
echo -e "${BOLD}  ─── VARIABLES (non-sensitive) ───────────────────────────${NC}"
echo -e "  Go to: ${CYAN}Settings → Secrets and variables → Actions → Variables tab${NC}"
echo -e "  Click ${BOLD}'New repository variable'${NC} for each one below:"
echo ""
echo -e "  ${YELLOW}AWS_REGION${NC}              =  $AWS_REGION"
echo -e "  ${YELLOW}ECR_API_URL${NC}             =  $ECR_API_URL"
echo -e "  ${YELLOW}ECR_FRONTEND_URL${NC}        =  $ECR_FRONTEND_URL"
echo -e "  ${YELLOW}ECS_CLUSTER${NC}             =  $ECS_CLUSTER"
echo -e "  ${YELLOW}ECS_API_SERVICE${NC}         =  $ECS_API_SERVICE"
echo -e "  ${YELLOW}ECS_WORKER_SERVICE${NC}      =  $ECS_WORKER_SERVICE"
echo -e "  ${YELLOW}ECS_FRONTEND_SERVICE${NC}    =  $ECS_FRONTEND_SERVICE"
echo -e "  ${YELLOW}ECS_MIGRATION_TASK_DEF${NC}  =  $ECS_MIGRATION_TASK"
echo -e "  ${YELLOW}ECS_PRIVATE_SUBNET${NC}      =  $PRIVATE_SUBNET"
echo -e "  ${YELLOW}ECS_SECURITY_GROUP${NC}      =  $API_SG"
echo ""

# Save secrets to file too
cat > "$SCRIPT_DIR/github-secrets.txt" << EOF
# ══════════════════════════════════════════════════════════
# GitHub Secrets — paste these into your GitHub repository
# Generated on $(date)
# ══════════════════════════════════════════════════════════

── SECRETS (Settings → Secrets and variables → Actions → Secrets) ──

AWS_ACCESS_KEY_ID     = $(aws configure get aws_access_key_id)
AWS_SECRET_ACCESS_KEY = $(aws configure get aws_secret_access_key)

── VARIABLES (Settings → Secrets and variables → Actions → Variables) ──

AWS_REGION              = $AWS_REGION
ECR_API_URL             = $ECR_API_URL
ECR_FRONTEND_URL        = $ECR_FRONTEND_URL
ECS_CLUSTER             = $ECS_CLUSTER
ECS_API_SERVICE         = $ECS_API_SERVICE
ECS_WORKER_SERVICE      = $ECS_WORKER_SERVICE
ECS_FRONTEND_SERVICE    = $ECS_FRONTEND_SERVICE
ECS_MIGRATION_TASK_DEF  = $ECS_MIGRATION_TASK
ECS_PRIVATE_SUBNET      = $PRIVATE_SUBNET
ECS_SECURITY_GROUP      = $API_SG

── APP INFO ──

App URL:              https://${ALB_URL#https://}
Admin Email:          $ADMIN_EMAIL
Cognito User Pool ID: $COGNITO_POOL_ID
Cognito Client ID:    $COGNITO_CLIENT_ID
EOF

echo -e "  ${GREEN}(All values also saved to: github-secrets.txt)${NC}"
echo ""
echo -e "${BOLD}  After adding secrets to GitHub:${NC}"
echo -e "  1. Push any code change to the main branch"
echo -e "  2. GitHub Actions will automatically build and deploy"
echo -e "  3. Watch it at: ${CYAN}GitHub repo → Actions tab${NC}"
echo ""
echo -e "${BOLD}${GREEN}  Happy building! 🚀${NC}"
echo ""
