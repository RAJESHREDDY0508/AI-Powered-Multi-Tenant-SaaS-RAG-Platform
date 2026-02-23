# ============================================================
# terraform.tfvars.example
# Copy to terraform.tfvars (never commit to git)
# ============================================================

project_name = "rag-platform"
environment  = "prod"
aws_region   = "us-east-1"

# ── Networking ───────────────────────────────────────────────
vpc_cidr           = "10.0.0.0/16"
availability_zones = ["us-east-1a", "us-east-1b"]

# ── Domain (optional — leave empty to use ALB DNS name) ──────
domain_name   = "ragplatform.io"   # your domain
api_subdomain = "api"              # api.ragplatform.io
app_subdomain = "app"              # app.ragplatform.io

# ── Database ─────────────────────────────────────────────────
db_instance_class    = "db.t3.medium"
db_name              = "ragplatform"
db_username          = "rag_admin"
db_allocated_storage = 50
db_multi_az          = true

# ── Cache ────────────────────────────────────────────────────
redis_node_type = "cache.t3.micro"

# ── Vector Store — Weaviate Cloud ─────────────────────────────
# Sign up at https://console.weaviate.cloud and create a cluster
weaviate_url     = "jzyhqzfstvgg2usvemr5g.c0.us-east1.gcp.weaviate.cloud"
weaviate_api_key = "WjRUM0ZDeFluMmUxbzVrdl90bVhYOGlIQjRLellaKzBDanEzMDNscisvY1JDdkwrbkgyQ09wcGFRSlFrPV92MjAw"

# ── AI Keys ──────────────────────────────────────────────────
openai_api_key    = "ssk-proj-twz7Ub1r6f0ZunHGoXoi1A_mPmM32Fx1IH5RAb45uvcq5I0q-q8OThEiwhUttNTavV3PXfhz8HT3BlbkFJxOFbRjHndqlfxLuMTpULuB-fRpaJ9G3a4DHU419nOZuVS0QfpNZXHD9lfmz5uYhaN7HrxtqYwA"
cohere_api_key    = "WjRUM0ZDeFluMmUxbzVrdl90bVhYOGlIQjRLellaKzBDanEzMDNscisvY1JDdkwrbkgyQ09wcGFRSlFrPV92MjAw"           # optional — for ReRank
langsmith_api_key = "ls__..."      # optional — for LLM tracing

# ── ECS Sizing ───────────────────────────────────────────────
api_cpu            = 1024   # 1 vCPU
api_memory         = 2048   # 2 GB
api_desired_count  = 2
worker_cpu         = 1024
worker_memory      = 2048
worker_desired_count = 1
frontend_cpu       = 512
frontend_memory    = 1024
