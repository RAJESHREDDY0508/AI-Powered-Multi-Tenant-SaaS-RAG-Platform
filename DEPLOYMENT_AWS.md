# AWS Deployment Guide — Step by Step

> **Read this first:** This guide deploys your RAG platform to AWS.
> Follow every step in order. Do not skip steps.
> Total time: ~1.5 hours on first deploy.

---

## What You Will Deploy

```
Your Browser
     │
     ▼
AWS Load Balancer  (public HTTPS endpoint)
     │
     ├──► /api/*  ──► API Server  (ECS container running FastAPI)
     │                    │
     └──► /*      ──► Frontend   (ECS container running Next.js)
                          │
                 ┌────────┴────────┐
                 ▼                 ▼
          PostgreSQL DB        Redis Cache
          (AWS RDS)        (AWS ElastiCache)
                 │
          ┌──────┴──────┐
          ▼             ▼
    File Storage    Vector DB
      (AWS S3)   (Weaviate Cloud)
          │
       Cognito
    (Login/Auth)
```

---

## PART 1 — Install Required Tools on Your Computer

### Step 1.1 — Install AWS CLI

The AWS CLI lets you control AWS from your terminal.

**On Windows (PowerShell as Administrator):**
```powershell
msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2.msi
```

**On Mac:**
```bash
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o AWSCLIV2.pkg
sudo installer -pkg AWSCLIV2.pkg -target /
```

**Verify it works:**
```bash
aws --version
```
You should see: `aws-cli/2.x.x Python/3.x.x ...`

---

### Step 1.2 — Install Terraform

Terraform creates all your AWS infrastructure automatically.

**On Windows (PowerShell as Administrator):**
```powershell
winget install HashiCorp.Terraform
```

**On Mac:**
```bash
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
```

**Verify it works:**
```bash
terraform --version
```
You should see: `Terraform v1.6.x or higher`

---

### Step 1.3 — Install Docker

Docker packages your application into containers.

- **Windows/Mac:** Download from https://www.docker.com/products/docker-desktop and install
- After installing, start Docker Desktop

**Verify it works:**
```bash
docker --version
```
You should see: `Docker version 24.x or higher`

---

### Step 1.4 — Install Git (if not already installed)

```bash
git --version
```
If not installed: https://git-scm.com/downloads

---

## PART 2 — Set Up Your AWS Account

### Step 2.1 — Create an AWS Account (if you don't have one)

1. Go to https://aws.amazon.com
2. Click **"Create an AWS Account"**
3. Follow the signup steps (requires a credit card)
4. Choose the **Free Tier** option

---

### Step 2.2 — Create an IAM User (for safe deployment)

**Why:** Never use your root AWS account for deployments. Create a separate user.

1. Go to https://console.aws.amazon.com/iam
2. Click **"Users"** in the left menu
3. Click **"Create user"**
4. Username: `rag-deployer`
5. Click **"Next"**
6. Click **"Attach policies directly"**
7. Search for `AdministratorAccess` and check the box
8. Click **"Next"** → **"Create user"**

**Now create access keys for this user:**
1. Click on the user `rag-deployer` you just created
2. Click the **"Security credentials"** tab
3. Scroll down to **"Access keys"** → Click **"Create access key"**
4. Choose **"Command Line Interface (CLI)"**
5. Check the confirmation box → Click **"Next"** → **"Create access key"**
6. **IMPORTANT:** Copy the **Access key ID** and **Secret access key** — you won't see the secret again!

---

### Step 2.3 — Connect AWS CLI to Your Account

Run this command and paste your keys when asked:

```bash
aws configure
```

Enter the following when prompted:
```
AWS Access Key ID [None]: PASTE_YOUR_ACCESS_KEY_HERE
AWS Secret Access Key [None]: PASTE_YOUR_SECRET_KEY_HERE
Default region name [None]: us-east-1
Default output format [None]: json
```

**Verify it worked:**
```bash
aws sts get-caller-identity
```

Expected output:
```json
{
    "UserId": "AIDA...",
    "Account": "123456789012",
    "Arn": "arn:aws:iam::123456789012:user/rag-deployer"
}
```

If you see your Account number, you're connected. ✅

---

## PART 3 — Get Your External API Keys

You need these before running Terraform. Get them all now.

### Step 3.1 — OpenAI API Key

1. Go to https://platform.openai.com/api-keys
2. Click **"Create new secret key"**
3. Name it: `rag-platform`
4. Click **"Create secret key"**
5. Copy the key (starts with `sk-...`) — **save it, you won't see it again**

---

### Step 3.2 — Weaviate Cloud (Vector Database)

1. Go to https://console.weaviate.cloud
2. Click **"Sign up"** (free account)
3. After signing in, click **"Create cluster"**
4. Choose **"Free sandbox"** tier
5. Give it a name: `rag-platform`
6. Click **"Create"** and wait ~2 minutes
7. Once created, click on your cluster
8. Copy the **Cluster URL** — looks like: `https://abc123.weaviate.network`
9. Click **"API Keys"** tab → Copy the API key

---

### Step 3.3 — Cohere API Key (optional — for better search results)

1. Go to https://dashboard.cohere.com/api-keys
2. Sign up for a free account
3. Copy your API key from the dashboard

---

## PART 4 — Prepare the Project

### Step 4.1 — Open a terminal in your project folder

```bash
cd C:\Users\rajes\Desktop\projects\AI-Powered-Multi-Tenant-SaaS-RAG-Platform
```

Confirm you're in the right place:
```bash
ls
```
You should see: `backend/`, `frontend/`, `infra/`, `docker-compose.yml`, etc.

---

### Step 4.2 — Give execution permission to scripts

**On Mac/Linux:**
```bash
chmod +x infra/scripts/bootstrap-state.sh
chmod +x backend/scripts/entrypoint.sh
chmod +x backend/scripts/worker-entrypoint.sh
chmod +x backend/scripts/run-migrations.sh
```

**On Windows (PowerShell) — scripts run in Linux containers so this is fine, skip this step.**

---

## PART 5 — Create Terraform State Storage

**What this does:** Creates an S3 bucket in AWS where Terraform saves its records of what it created. This is required before you can run Terraform.

### Step 5.1 — Run the bootstrap script

**On Mac/Linux:**
```bash
export AWS_REGION=us-east-1
bash infra/scripts/bootstrap-state.sh
```

**On Windows (PowerShell):**
```powershell
$env:AWS_REGION = "us-east-1"
$env:PROJECT_NAME = "rag-platform"
# Run these AWS commands manually:

aws s3api create-bucket --bucket rag-platform-tfstate --region us-east-1

aws s3api put-bucket-versioning --bucket rag-platform-tfstate --versioning-configuration Status=Enabled

aws s3api put-public-access-block --bucket rag-platform-tfstate --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws dynamodb create-table `
  --table-name rag-platform-tfstate-lock `
  --attribute-definitions AttributeName=LockID,AttributeType=S `
  --key-schema AttributeName=LockID,KeyType=HASH `
  --billing-mode PAY_PER_REQUEST `
  --region us-east-1
```

**Expected output:**
```
=== Bootstrapping Terraform State Backend ===
[OK] S3 bucket configured
[OK] DynamoDB table configured
=== Bootstrap complete ===
```

---

## PART 6 — Configure Terraform Variables

### Step 6.1 — Create your variables file

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
```

### Step 6.2 — Edit the variables file

Open `infra/terraform/terraform.tfvars` in any text editor and fill in your values:

```bash
# On Mac/Linux:
nano terraform.tfvars

# On Windows, open in Notepad:
notepad terraform.tfvars
```

Replace EVERY value in ALL CAPS with your actual values:

```hcl
project_name = "rag-platform"
environment  = "prod"
aws_region   = "us-east-1"

# Network settings (leave as-is)
vpc_cidr           = "10.0.0.0/16"
availability_zones = ["us-east-1a", "us-east-1b"]

# OPTIONAL: If you own a domain (e.g. ragplatform.io), put it here.
# If you DON'T have a domain, leave it as empty string "":
domain_name   = ""
api_subdomain = "api"
app_subdomain = "app"

# Database settings (leave as-is)
db_instance_class    = "db.t3.medium"
db_name              = "ragplatform"
db_username          = "rag_admin"
db_allocated_storage = 50
db_multi_az          = true

# Cache settings (leave as-is)
redis_node_type = "cache.t3.micro"

# ↓↓ FILL THESE IN ↓↓

# From Step 3.2 — Weaviate Cloud
weaviate_url     = "https://your-cluster-id.weaviate.network"
weaviate_api_key = "your-weaviate-api-key-here"

# From Step 3.1 — OpenAI
openai_api_key = "sk-your-openai-key-here"

# From Step 3.3 — Cohere (optional, put "" if you don't have it)
cohere_api_key = "your-cohere-key-here"

# LangSmith (optional, leave "" if you don't have it)
langsmith_api_key = ""

# Server sizes (leave as-is for now)
api_cpu            = 1024
api_memory         = 2048
api_desired_count  = 2
worker_cpu         = 1024
worker_memory      = 2048
worker_desired_count = 1
frontend_cpu       = 512
frontend_memory    = 1024
```

Save and close the file.

---

## PART 7 — Run Terraform (Create AWS Infrastructure)

**What this does:** Creates your entire AWS infrastructure — VPC, database, Redis, load balancer, ECS cluster, Cognito, S3, ECR, and everything else. This takes about 15-20 minutes.

### Step 7.1 — Initialize Terraform

```bash
cd infra/terraform
terraform init
```

Expected output (last few lines):
```
Terraform has been successfully initialized!

You may now begin working with Terraform. Try running "terraform plan"
```

If you see an error about the S3 bucket not existing, go back to Part 5.

---

### Step 7.2 — Preview what Terraform will create

```bash
terraform plan
```

This shows you everything Terraform will create. Read through it. You should see resources like:
- `aws_vpc.main`
- `aws_db_instance.main`
- `aws_elasticache_replication_group.main`
- `aws_ecs_cluster.main`
- `aws_cognito_user_pool.main`
- etc.

At the bottom you should see:
```
Plan: 60+ to add, 0 to change, 0 to destroy.
```

---

### Step 7.3 — Create all the infrastructure

```bash
terraform apply
```

Terraform will ask you to confirm. Type `yes` and press Enter:
```
Do you want to perform these actions?
  Terraform will perform the following actions:
  ...
  Enter a value: yes
```

Wait 15-20 minutes. You'll see resources being created line by line.

When done, you'll see:
```
Apply complete! Resources: 60+ added, 0 changed, 0 destroyed.

Outputs:

alb_dns_name = "https://rag-platform-prod-alb-xxxx.us-east-1.elb.amazonaws.com"
cognito_user_pool_id = "us-east-1_xxxxxxxx"
cognito_client_id = "xxxxxxxxxxxxxxxxxxxxxxxxxx"
ecr_api_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/rag-platform-prod/api"
ecr_frontend_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/rag-platform-prod/frontend"
ecs_cluster_name = "rag-platform-prod-cluster"
...
```

### Step 7.4 — Save the outputs to a file

**This is important** — you'll need these values in later steps.

```bash
terraform output > ../../terraform-outputs.txt
cat ../../terraform-outputs.txt
```

---

## PART 8 — Build and Push Docker Images

**What this does:** Packages your application code into Docker images and uploads them to AWS ECR (like Docker Hub but private and in AWS).

### Step 8.1 — Go back to the project root

```bash
cd ../..
# You should now be in: AI-Powered-Multi-Tenant-SaaS-RAG-Platform/
```

### Step 8.2 — Set up environment variables

Copy these values from the `terraform-outputs.txt` file you saved.

**On Mac/Linux:**
```bash
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Paste your actual ECR URLs from terraform output:
export ECR_API_URL="123456789012.dkr.ecr.us-east-1.amazonaws.com/rag-platform-prod/api"
export ECR_FRONTEND_URL="123456789012.dkr.ecr.us-east-1.amazonaws.com/rag-platform-prod/frontend"
export IMAGE_TAG="v1.0.0"
```

**On Windows (PowerShell):**
```powershell
$AWS_REGION = "us-east-1"
$AWS_ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text)

# Paste your actual ECR URLs from terraform output:
$ECR_API_URL = "123456789012.dkr.ecr.us-east-1.amazonaws.com/rag-platform-prod/api"
$ECR_FRONTEND_URL = "123456789012.dkr.ecr.us-east-1.amazonaws.com/rag-platform-prod/frontend"
$IMAGE_TAG = "v1.0.0"
```

### Step 8.3 — Log Docker in to AWS ECR

**On Mac/Linux:**
```bash
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
```

**On Windows (PowerShell):**
```powershell
$loginPassword = aws ecr get-login-password --region $AWS_REGION
$loginPassword | docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
```

Expected output:
```
Login Succeeded
```

---

### Step 8.4 — Build the API (backend) Docker image

This packages your FastAPI backend into a Docker image. Takes 3-8 minutes.

**On Mac/Linux:**
```bash
docker build \
  --target api \
  --tag "$ECR_API_URL:$IMAGE_TAG" \
  --tag "$ECR_API_URL:latest" \
  ./backend
```

**On Windows (PowerShell):**
```powershell
docker build `
  --target api `
  --tag "${ECR_API_URL}:${IMAGE_TAG}" `
  --tag "${ECR_API_URL}:latest" `
  ./backend
```

Watch for errors. When complete you'll see:
```
=> exporting to image
=> naming to 123456789012.dkr.ecr.us-east-1.amazonaws.com/.../api:v1.0.0
```

---

### Step 8.5 — Push the API image to AWS

**On Mac/Linux:**
```bash
docker push "$ECR_API_URL:$IMAGE_TAG"
docker push "$ECR_API_URL:latest"
```

**On Windows (PowerShell):**
```powershell
docker push "${ECR_API_URL}:${IMAGE_TAG}"
docker push "${ECR_API_URL}:latest"
```

This uploads ~1-2 GB. Takes 2-5 minutes depending on your internet speed.

Expected output:
```
v1.0.0: digest: sha256:abc123... size: 1234
latest: digest: sha256:abc123... size: 1234
```

---

### Step 8.6 — Build the Frontend Docker image

**On Mac/Linux:**
```bash
docker build \
  --build-arg NEXT_PUBLIC_APP_VERSION=$IMAGE_TAG \
  --tag "$ECR_FRONTEND_URL:$IMAGE_TAG" \
  --tag "$ECR_FRONTEND_URL:latest" \
  ./frontend
```

**On Windows (PowerShell):**
```powershell
docker build `
  --build-arg NEXT_PUBLIC_APP_VERSION=$IMAGE_TAG `
  --tag "${ECR_FRONTEND_URL}:${IMAGE_TAG}" `
  --tag "${ECR_FRONTEND_URL}:latest" `
  ./frontend
```

---

### Step 8.7 — Push the Frontend image to AWS

**On Mac/Linux:**
```bash
docker push "$ECR_FRONTEND_URL:$IMAGE_TAG"
docker push "$ECR_FRONTEND_URL:latest"
```

**On Windows (PowerShell):**
```powershell
docker push "${ECR_FRONTEND_URL}:${IMAGE_TAG}"
docker push "${ECR_FRONTEND_URL}:latest"
```

✅ Both images are now in AWS and ready to run.

---

## PART 9 — Run Database Migrations

**What this does:** Creates all the database tables your application needs. This runs your SQL files (`backend/migrations/001_*.sql`, `002_*.sql`, `003_*.sql`) against the RDS database.

### Step 9.1 — Get the values needed for the migration task

From your `terraform-outputs.txt` file, find:
- `ecs_cluster_name`
- `ecs_migration_task_definition`

You also need a subnet ID and security group ID:

**On Mac/Linux:**
```bash
cd infra/terraform

# Get the private subnet ID
PRIVATE_SUBNET=$(terraform state show 'aws_subnet.private[0]' 2>/dev/null | grep '"id"' | head -1 | awk '{print $3}' | tr -d '"')
echo "Private Subnet: $PRIVATE_SUBNET"

# Get the API security group ID
API_SG=$(terraform state show aws_security_group.ecs_api 2>/dev/null | grep '"id"' | head -1 | awk '{print $3}' | tr -d '"')
echo "API Security Group: $API_SG"

# Get cluster name
ECS_CLUSTER=$(terraform output -raw ecs_cluster_name)
echo "ECS Cluster: $ECS_CLUSTER"

# Get migration task definition
MIGRATION_TASK=$(terraform output -raw ecs_migration_task_definition)
echo "Migration Task: $MIGRATION_TASK"

cd ../..
```

If the above commands don't work cleanly, you can find the values in the AWS Console:
- **Subnet:** Go to VPC → Subnets → find "private-1" → copy the Subnet ID
- **Security Group:** Go to EC2 → Security Groups → find "ecs-api-sg" → copy the Group ID

### Step 9.2 — Run the migration task in AWS ECS

**On Mac/Linux:**
```bash
TASK_ARN=$(aws ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$MIGRATION_TASK" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$PRIVATE_SUBNET],securityGroups=[$API_SG],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' \
  --output text)

echo "Migration task started: $TASK_ARN"
```

**On Windows (PowerShell):**
```powershell
# Fill in your actual values here:
$ECS_CLUSTER = "rag-platform-prod-cluster"
$MIGRATION_TASK = "rag-platform-prod-migration:1"
$PRIVATE_SUBNET = "subnet-xxxxxxxxxxxxxxxxx"   # From AWS Console
$API_SG = "sg-xxxxxxxxxxxxxxxxx"               # From AWS Console

$TASK_ARN = aws ecs run-task `
  --cluster $ECS_CLUSTER `
  --task-definition $MIGRATION_TASK `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[$PRIVATE_SUBNET],securityGroups=[$API_SG],assignPublicIp=DISABLED}" `
  --query 'tasks[0].taskArn' `
  --output text

Write-Host "Migration task started: $TASK_ARN"
```

### Step 9.3 — Wait for the migration to complete

```bash
echo "Waiting for migration to complete... (this takes 1-3 minutes)"

aws ecs wait tasks-stopped \
  --cluster "$ECS_CLUSTER" \
  --tasks "$TASK_ARN"

echo "Migration task finished."
```

### Step 9.4 — Check if migration succeeded

```bash
EXIT_CODE=$(aws ecs describe-tasks \
  --cluster "$ECS_CLUSTER" \
  --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' \
  --output text)

echo "Exit code: $EXIT_CODE"
```

- If exit code is `0` → ✅ Migration succeeded, continue to next step
- If exit code is `1` or any other number → ❌ Migration failed

**If migration failed, check the logs:**
```bash
# Find the log stream
aws logs describe-log-streams \
  --log-group-name "/ecs/rag-platform-prod/migration" \
  --order-by LastEventTime \
  --descending \
  --max-items 1 \
  --query 'logStreams[0].logStreamName' \
  --output text
```

Then view the logs in the AWS Console:
1. Go to CloudWatch → Log groups
2. Find `/ecs/rag-platform-prod/migration`
3. Click the most recent log stream
4. Read the error message

---

## PART 10 — Deploy Your Application

**What this does:** Tells ECS to start running your newly built Docker images.

### Step 10.1 — Get service names

From your `terraform-outputs.txt`:
```
ECS_API_SERVICE = "rag-platform-prod-api"
ECS_WORKER_SERVICE = "rag-platform-prod-worker"
ECS_FRONTEND_SERVICE = "rag-platform-prod-frontend"
```

Set them as variables:

**On Mac/Linux:**
```bash
cd infra/terraform
ECS_CLUSTER=$(terraform output -raw ecs_cluster_name)
ECS_API_SERVICE=$(terraform output -raw ecs_api_service_name)
ECS_WORKER_SERVICE=$(terraform output -raw ecs_worker_service_name)
ECS_FRONTEND_SERVICE=$(terraform output -raw ecs_frontend_service_name)
cd ../..
```

**On Windows (PowerShell):**
```powershell
cd infra/terraform
$ECS_CLUSTER = terraform output -raw ecs_cluster_name
$ECS_API_SERVICE = terraform output -raw ecs_api_service_name
$ECS_WORKER_SERVICE = terraform output -raw ecs_worker_service_name
$ECS_FRONTEND_SERVICE = terraform output -raw ecs_frontend_service_name
cd ../..
```

### Step 10.2 — Deploy the API service

```bash
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_API_SERVICE" \
  --force-new-deployment \
  --output text \
  --query 'service.serviceName'
```

Expected output: `rag-platform-prod-api`

### Step 10.3 — Deploy the Worker service

```bash
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_WORKER_SERVICE" \
  --force-new-deployment \
  --output text \
  --query 'service.serviceName'
```

### Step 10.4 — Deploy the Frontend service

```bash
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_FRONTEND_SERVICE" \
  --force-new-deployment \
  --output text \
  --query 'service.serviceName'
```

### Step 10.5 — Wait for services to be healthy

The deployments take 3-5 minutes. Monitor them:

```bash
# Watch API service (press Ctrl+C to stop watching)
watch -n 10 "aws ecs describe-services \
  --cluster $ECS_CLUSTER \
  --services $ECS_API_SERVICE \
  --query 'services[0].{Running:runningCount,Desired:desiredCount,Status:deployments[0].status}' \
  --output table"
```

**On Windows (PowerShell — run this every 30 seconds manually):**
```powershell
aws ecs describe-services `
  --cluster $ECS_CLUSTER `
  --services $ECS_API_SERVICE `
  --query 'services[0].{Running:runningCount,Desired:desiredCount,Pending:pendingCount}' `
  --output table
```

Wait until you see `Running` count equals `Desired` count (e.g., Running: 2, Desired: 2).

---

## PART 11 — Create Your First Admin User

**What this does:** Creates a login account in AWS Cognito so you can sign into your application.

### Step 11.1 — Get your Cognito User Pool ID

```bash
cd infra/terraform
POOL_ID=$(terraform output -raw cognito_user_pool_id)
echo "Pool ID: $POOL_ID"
cd ../..
```

### Step 11.2 — Create the admin user

Replace `admin@yourcompany.com` with YOUR actual email address, and `YourSecurePassword123!` with a strong password.

**Password requirements:** At least 12 characters, with uppercase, lowercase, and numbers.

**On Mac/Linux:**
```bash
POOL_ID="us-east-1_xxxxxxxxx"   # From terraform output

aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" \
  --username "admin@yourcompany.com" \
  --user-attributes \
    Name=email,Value=admin@yourcompany.com \
    Name=email_verified,Value=true \
    Name="custom:role",Value=admin \
    Name="custom:tenant_id",Value=tenant_001 \
  --temporary-password "TempPass123!" \
  --message-action SUPPRESS

echo "User created. Setting permanent password..."

aws cognito-idp admin-set-user-password \
  --user-pool-id "$POOL_ID" \
  --username "admin@yourcompany.com" \
  --password "YourSecurePassword123!" \
  --permanent

echo "✅ Admin user ready!"
echo "Email: admin@yourcompany.com"
echo "Password: YourSecurePassword123!"
```

**On Windows (PowerShell):**
```powershell
$POOL_ID = "us-east-1_xxxxxxxxx"   # From terraform output

aws cognito-idp admin-create-user `
  --user-pool-id $POOL_ID `
  --username "admin@yourcompany.com" `
  --user-attributes `
    Name=email,Value=admin@yourcompany.com `
    Name=email_verified,Value=true `
    "Name=custom:role,Value=admin" `
    "Name=custom:tenant_id,Value=tenant_001" `
  --temporary-password "TempPass123!" `
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password `
  --user-pool-id $POOL_ID `
  --username "admin@yourcompany.com" `
  --password "YourSecurePassword123!" `
  --permanent
```

Expected output:
```json
{
    "User": {
        "Username": "admin@yourcompany.com",
        "UserStatus": "CONFIRMED",
        ...
    }
}
```

---

## PART 12 — Test Your Deployment

### Step 12.1 — Get your application URL

```bash
cd infra/terraform
ALB_URL=$(terraform output -raw alb_dns_name)
echo "Your app URL: $ALB_URL"
cd ../..
```

The URL looks like: `https://rag-platform-prod-alb-1234567890.us-east-1.elb.amazonaws.com`

### Step 12.2 — Test the API health endpoint

```bash
curl -k "$ALB_URL/health"
```

Expected response:
```json
{"status": "ok", "service": "rag-platform-api"}
```

If you get an error, wait 2 more minutes and try again — the containers might still be starting.

### Step 12.3 — Test the API readiness (checks database connection)

```bash
curl -k "$ALB_URL/ready"
```

Expected response:
```json
{"status": "ready", "database": {"status": "ok"}}
```

### Step 12.4 — Open the frontend in your browser

Open your browser and go to: `https://YOUR-ALB-URL-HERE`

You should see the RAG Platform login page.

> **Note:** Your browser may show a security warning because the SSL certificate is self-signed (when no custom domain is set). Click **"Advanced"** → **"Proceed anyway"**.

### Step 12.5 — Log in with your admin account

1. Enter the email and password you created in Part 11
2. Click **"Sign in"**
3. You should see the dashboard ✅

---

## PART 13 — Set Up Automatic Deployments (GitHub Actions CI/CD)

**What this does:** Every time you push code to GitHub, it automatically tests, builds, and deploys your app.

### Step 13.1 — Push your project to GitHub

If you haven't already:
1. Create a new **private** repository on GitHub at https://github.com/new
2. Run:
```bash
cd C:\Users\rajes\Desktop\projects\AI-Powered-Multi-Tenant-SaaS-RAG-Platform
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
git push -u origin main
```

### Step 13.2 — Add GitHub Repository Variables

In your GitHub repository:
1. Go to **Settings** → **Secrets and variables** → **Actions**
2. Click the **"Variables"** tab
3. Click **"New repository variable"** for each of these:

| Variable Name | Value (from terraform-outputs.txt) |
|--------------|-------------------------------------|
| `AWS_REGION` | `us-east-1` |
| `ECR_API_URL` | paste `ecr_api_url` output |
| `ECR_FRONTEND_URL` | paste `ecr_frontend_url` output |
| `ECS_CLUSTER` | paste `ecs_cluster_name` output |
| `ECS_API_SERVICE` | paste `ecs_api_service_name` output |
| `ECS_WORKER_SERVICE` | paste `ecs_worker_service_name` output |
| `ECS_FRONTEND_SERVICE` | paste `ecs_frontend_service_name` output |
| `ECS_MIGRATION_TASK_DEF` | paste `ecs_migration_task_definition` output |
| `ECS_PRIVATE_SUBNET` | your private subnet ID (e.g. `subnet-xxx`) |
| `ECS_SECURITY_GROUP` | your API security group ID (e.g. `sg-xxx`) |

### Step 13.3 — Add AWS Credentials as GitHub Secrets

Still in Settings → Secrets and variables → Actions, click the **"Secrets"** tab:

| Secret Name | Value |
|------------|-------|
| `AWS_ACCESS_KEY_ID` | Your AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret key |

### Step 13.4 — Update the workflow file to use key-based auth

Open `.github/workflows/deploy.yml` and find this section:
```yaml
- name: Configure AWS credentials (OIDC)
  if: ${{ vars.AWS_ROLE_ARN != '' }}
```

Add a step that always uses access keys (simpler for now):

Actually, since `AWS_ROLE_ARN` is not set as a variable, the workflow will automatically fall through to the keys-based authentication. No changes needed.

### Step 13.5 — Trigger your first automated deployment

```bash
git add .
git commit -m "set up automated deployment"
git push origin main
```

Now go to your GitHub repository → **Actions** tab. You'll see a workflow running automatically.

Each step in the workflow should turn green ✅. If any step fails (red ❌), click on it to see the error logs.

---

## PART 14 — View Monitoring & Logs

### View application logs in real time

```bash
# API logs
aws logs tail /ecs/rag-platform-prod/api --follow

# Worker logs (document processing)
aws logs tail /ecs/rag-platform-prod/worker --follow

# Frontend logs
aws logs tail /ecs/rag-platform-prod/frontend --follow
```

Press `Ctrl+C` to stop following.

### View CloudWatch Dashboard

1. Go to https://console.aws.amazon.com/cloudwatch
2. Click **"Dashboards"** in the left menu
3. Click **"rag-platform-prod-overview"**
4. You'll see charts for CPU, memory, requests, and latency

---

## Summary: All Commands in Order

Here's a quick reference of every command, in order:

```bash
# ── PART 5: Bootstrap state ──────────────────────────────────
bash infra/scripts/bootstrap-state.sh

# ── PART 6: Configure vars ───────────────────────────────────
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
# Edit terraform.tfvars with your API keys

# ── PART 7: Deploy infrastructure ────────────────────────────
cd infra/terraform
terraform init
terraform plan
terraform apply      # type "yes" when asked
terraform output > ../../terraform-outputs.txt
cd ../..

# ── PART 8: Build & push images ──────────────────────────────
export ECR_API_URL="YOUR_ECR_API_URL_FROM_OUTPUT"
export ECR_FRONTEND_URL="YOUR_ECR_FRONTEND_URL_FROM_OUTPUT"
export IMAGE_TAG="v1.0.0"
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com"
docker build --target api -t "$ECR_API_URL:$IMAGE_TAG" -t "$ECR_API_URL:latest" ./backend
docker push "$ECR_API_URL:$IMAGE_TAG" && docker push "$ECR_API_URL:latest"
docker build -t "$ECR_FRONTEND_URL:$IMAGE_TAG" -t "$ECR_FRONTEND_URL:latest" ./frontend
docker push "$ECR_FRONTEND_URL:$IMAGE_TAG" && docker push "$ECR_FRONTEND_URL:latest"

# ── PART 9: Run migrations ────────────────────────────────────
# (see detailed steps above — requires subnet + SG IDs)

# ── PART 10: Deploy services ──────────────────────────────────
aws ecs update-service --cluster rag-platform-prod-cluster --service rag-platform-prod-api --force-new-deployment
aws ecs update-service --cluster rag-platform-prod-cluster --service rag-platform-prod-worker --force-new-deployment
aws ecs update-service --cluster rag-platform-prod-cluster --service rag-platform-prod-frontend --force-new-deployment

# ── PART 11: Create admin user ────────────────────────────────
# (see detailed steps above — requires POOL_ID)

# ── PART 12: Test ─────────────────────────────────────────────
curl -k "$(cd infra/terraform && terraform output -raw alb_dns_name)/health"
```

---

## Troubleshooting

### "terraform init fails — bucket does not exist"
→ Run Part 5 first: `bash infra/scripts/bootstrap-state.sh`

### "docker build fails — Dockerfile not found"
→ Make sure you're in the project root folder when running docker commands

### "docker push fails — no credentials"
→ Run the `aws ecr get-login-password | docker login ...` command again (Step 8.3)

### "Migration task fails with exit code 1"
→ Check logs in AWS Console: CloudWatch → Log groups → `/ecs/rag-platform-prod/migration`
→ Most common cause: DATABASE_URL in Secrets Manager doesn't match RDS endpoint

### "ECS service stuck — running: 0, desired: 2"
→ Your container is crashing at startup
→ Check logs: `aws logs tail /ecs/rag-platform-prod/api --follow`
→ Most common cause: missing environment variable or bad API key

### "Browser shows SSL error"
→ Normal when you have no custom domain. Click "Advanced" → "Proceed anyway"
→ To fix permanently: buy a domain and add it to `domain_name` in terraform.tfvars

### "curl /health returns 503 Service Unavailable"
→ ECS containers not healthy yet — wait 3-5 minutes and try again
→ If still failing: `aws ecs describe-services --cluster rag-platform-prod-cluster --services rag-platform-prod-api`
→ Look at the `events` list for error messages

### "Login page shows but login fails"
→ Make sure you ran Step 11.2 to create the user
→ Check the pool ID: `terraform output cognito_user_pool_id`
→ Verify user exists: `aws cognito-idp list-users --user-pool-id YOUR_POOL_ID`

---

## To Tear Down Everything (Delete All AWS Resources)

⚠️ **WARNING: This deletes your database and all data permanently.**

```bash
cd infra/terraform
terraform destroy
# Type "yes" when asked
```
