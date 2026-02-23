# Deploy to AWS — 3 Steps

> **Time required:** ~25 minutes (mostly waiting for AWS to provision)

---

## Step 1 — Run the Setup Script (One Time Only)

This script creates all your AWS infrastructure and prints your GitHub secrets.

```bash
bash setup.sh
```

It will ask you 6 questions:

| Question | Example Answer |
|---|---|
| AWS Region | `us-east-1` |
| Project name | `rag-platform` |
| OpenAI API key | `sk-...` |
| Weaviate URL | `https://xyz.weaviate.network` |
| Weaviate API key | `...` |
| Cohere API key | `...` |
| Admin email | `admin@yourcompany.com` |
| Admin password | `MySecurePass123!` |

When it finishes (≈20 min), it prints something like:

```
========================================
  ADD THESE SECRETS TO GITHUB
========================================

Go to: https://github.com/YOUR/REPO/settings/secrets/actions

Secrets (Settings → Secrets → Actions → New repository secret):
  AWS_ACCESS_KEY_ID     = AKIA...
  AWS_SECRET_ACCESS_KEY = ...

Variables (Settings → Variables → Actions → New repository variable):
  AWS_REGION            = us-east-1
  ECR_API_URL           = 123456789.dkr.ecr.us-east-1.amazonaws.com/rag-platform-api
  ECR_FRONTEND_URL      = 123456789.dkr.ecr.us-east-1.amazonaws.com/rag-platform-frontend
  ECS_CLUSTER           = rag-platform-cluster
  ECS_API_SERVICE       = rag-platform-api
  ECS_WORKER_SERVICE    = rag-platform-worker
  ECS_FRONTEND_SERVICE  = rag-platform-frontend
  ECS_MIGRATION_TASK_DEF = rag-platform-migration
  ECS_PRIVATE_SUBNET    = subnet-...
  ECS_SECURITY_GROUP    = sg-...

Your app is live at: https://rag-platform-alb-1234567890.us-east-1.elb.amazonaws.com
```

---

## Step 2 — Add Secrets to GitHub

1. Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions**

2. Add **Secrets** (click "New repository secret" for each):

   | Name | Value |
   |---|---|
   | `AWS_ACCESS_KEY_ID` | from script output |
   | `AWS_SECRET_ACCESS_KEY` | from script output |

3. Add **Variables** (click the **Variables** tab → "New repository variable" for each):

   | Name | Value |
   |---|---|
   | `AWS_REGION` | from script output |
   | `ECR_API_URL` | from script output |
   | `ECR_FRONTEND_URL` | from script output |
   | `ECS_CLUSTER` | from script output |
   | `ECS_API_SERVICE` | from script output |
   | `ECS_WORKER_SERVICE` | from script output |
   | `ECS_FRONTEND_SERVICE` | from script output |
   | `ECS_MIGRATION_TASK_DEF` | from script output |
   | `ECS_PRIVATE_SUBNET` | from script output |
   | `ECS_SECURITY_GROUP` | from script output |

> 💡 **Tip:** All values are also saved to `github-secrets.txt` in your project folder.

---

## Step 3 — Push Code → Auto Deploy

Every time you push to `main`, GitHub automatically:

1. ✅ Runs your tests
2. 🐳 Builds Docker images and pushes to ECR
3. 🗄️ Runs database migrations
4. 🚀 Deploys updated containers to ECS

```bash
git add .
git commit -m "my changes"
git push origin main
```

Watch the deployment at:
**GitHub → Your Repo → Actions tab**

---

## That's it! 🎉

Your app will be live at the URL printed by the setup script.

---

## Quick Reference

| Task | Command |
|---|---|
| Re-run setup (if something failed) | `bash setup.sh` |
| View live logs | AWS Console → CloudWatch → Log groups → `/ecs/rag-platform/api` |
| Trigger manual deploy | GitHub → Actions → "Deploy to AWS" → "Run workflow" |
| Tear down everything | `cd infra/terraform && terraform destroy` |
| SSH into container (debugging) | AWS Console → ECS → Cluster → Task → Connect |

---

## Troubleshooting

**Tests fail in GitHub Actions?**
→ Check the test output in the Actions tab. Likely a missing env variable.

**Docker push fails?**
→ Make sure `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are added as **Secrets** (not Variables).

**Migration fails?**
→ AWS Console → CloudWatch → Log groups → `/ecs/rag-platform/migration`

**Site not loading after deploy?**
→ Wait 2–3 minutes for ECS to finish health checks. If still failing, check:
→ AWS Console → ECS → Cluster → Services → API → Tasks → Logs

**Need to update an API key?**
→ AWS Console → Secrets Manager → `rag-platform/app-secrets` → Edit → update the value → redeploy via GitHub Actions
