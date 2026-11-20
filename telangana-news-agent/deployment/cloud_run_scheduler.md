# Deploying to Cloud Run Jobs + Cloud Scheduler

Two Cloud Run Jobs share one container image; two Cloud Scheduler jobs trigger them on
`Asia/Kolkata` schedules. Nothing depends on a local computer being on.

> Verify current pricing/free tiers for Cloud Run, Scheduler (3 free jobs per billing account
> at time of writing), Artifact Registry and Logging before launch. Set a budget alert;
> it notifies but does **not** cap spend.

## 0. Prerequisites

- `gcloud` CLI, a project with billing enabled, and Owner/Editor on it for setup.
- Supabase project created and `migrations/001_init.sql` applied (SQL editor → paste → run).
- Gmail credentials obtained (`scripts/gmail_auth.py`) for the approved sending mailbox.
- Dry-run output reviewed and approved by the team (acceptance criterion).

```bash
PROJECT_ID=your-project
REGION=asia-south1                      # Mumbai
REPO=telangana-news
IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/agent:$(date +%Y%m%d-%H%M)

gcloud config set project $PROJECT_ID
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com
```

## 1. Service accounts (least privilege)

```bash
# Identity the jobs run as: can read its secrets, nothing else.
gcloud iam service-accounts create news-agent-runtime --display-name="News agent runtime"
# Identity Cloud Scheduler uses to start the jobs.
gcloud iam service-accounts create news-agent-scheduler --display-name="News agent scheduler"

RUNTIME_SA=news-agent-runtime@$PROJECT_ID.iam.gserviceaccount.com
SCHED_SA=news-agent-scheduler@$PROJECT_ID.iam.gserviceaccount.com
```

## 2. Secrets

```bash
# Use GROQ_API_KEY or GEMINI_API_KEY (whichever provider you chose); swap it in SECRETS below too.
for NAME in DATABASE_URL GROQ_API_KEY GMAIL_CLIENT_ID GMAIL_CLIENT_SECRET GMAIL_REFRESH_TOKEN; do
  printf "Value for $NAME: "; read -rs VAL; echo
  printf '%s' "$VAL" | gcloud secrets create $NAME --data-file=- --replication-policy=automatic
  gcloud secrets add-iam-policy-binding $NAME \
    --member=serviceAccount:$RUNTIME_SA --role=roles/secretmanager.secretAccessor
done
# Optional: TAVILY_API_KEY the same way (and set tavily.enabled: true in config/pipeline.yaml).
```

Use the Supabase **Session pooler** connection string for `DATABASE_URL` (IPv4-reachable from
Cloud Run; the direct connection host is IPv6-only).

## 3. Build the image

```bash
gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION
gcloud builds submit --tag $IMAGE .
```

Config files (`config/*.yaml`) are baked into the image. Editing thresholds/keywords/sources =
edit YAML → rebuild → `gcloud run jobs update ... --image`. No code changes needed.

## 4. Create the two jobs

Non-secret settings go in env vars; start with `DRY_RUN=true` and only the test recipient.

```bash
COMMON_ENV="DRY_RUN=true,EMAIL_SENDER=alerts@yourdomain,EMAIL_TEAM_RECIPIENTS=you@yourdomain,EMAIL_REVIEWER_RECIPIENTS=you@yourdomain,EMAIL_RECIPIENT_ALLOWLIST=you@yourdomain"
SECRETS="DATABASE_URL=DATABASE_URL:latest,GROQ_API_KEY=GROQ_API_KEY:latest,GMAIL_CLIENT_ID=GMAIL_CLIENT_ID:latest,GMAIL_CLIENT_SECRET=GMAIL_CLIENT_SECRET:latest,GMAIL_REFRESH_TOKEN=GMAIL_REFRESH_TOKEN:latest"

gcloud run jobs create news-monitor --image=$IMAGE --region=$REGION \
  --service-account=$RUNTIME_SA --command=python --args=-m,app.monitor \
  --set-env-vars="$COMMON_ENV" --set-secrets="$SECRETS" \
  --memory=512Mi --cpu=1 --task-timeout=600s --max-retries=1

gcloud run jobs create news-digest --image=$IMAGE --region=$REGION \
  --service-account=$RUNTIME_SA --command=python --args=-m,app.digest \
  --set-env-vars="$COMMON_ENV" --set-secrets="$SECRETS" \
  --memory=512Mi --cpu=1 --task-timeout=900s --max-retries=2
```

Retries are safe: articles are de-duplicated by URL/content hash, and every email claims a unique
`notifications.idempotency_key` before sending, so a retried run never re-sends.

Test manually: `gcloud run jobs execute news-monitor --region=$REGION --wait`, then read logs.

## 5. Schedules (Asia/Kolkata)

```bash
for JOB in news-monitor news-digest; do
  gcloud run jobs add-iam-policy-binding $JOB --region=$REGION \
    --member=serviceAccount:$SCHED_SA --role=roles/run.invoker
done

gcloud scheduler jobs create http news-monitor-every-15m --location=$REGION \
  --schedule="*/15 * * * *" --time-zone="Asia/Kolkata" \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/news-monitor:run" \
  --http-method=POST --oauth-service-account-email=$SCHED_SA

gcloud scheduler jobs create http news-digest-0900 --location=$REGION \
  --schedule="0 9 * * *" --time-zone="Asia/Kolkata" \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/news-digest:run" \
  --http-method=POST --oauth-service-account-email=$SCHED_SA
```

Two scheduler jobs keeps within the commonly free allowance of three per billing account.

## 6. Monitoring failures

Logs are JSON with `severity`, so Cloud Logging filters work directly:

```
resource.type="cloud_run_job" severity>=ERROR
```

- Create a **log-based alert** on that filter (Monitoring → Alerting) to email the operator.
  It catches failed runs, Gmail auth errors, and sources failing 4+ consecutive polls.
- The `runs` table records every execution (`status` ok / partial / failed) for audit.
- Each digest footer lists sources currently failing.

## 7. Going live (only after the team approves a test digest)

```bash
gcloud run jobs update news-digest --region=$REGION \
  --update-env-vars="DRY_RUN=false,EMAIL_TEAM_RECIPIENTS=comms-team@yourdomain,EMAIL_RECIPIENT_ALLOWLIST=comms-team@yourdomain,you@yourdomain"
gcloud run jobs update news-monitor --region=$REGION --update-env-vars="DRY_RUN=false,..."
```

Keep `mode: review` in `config/alert_rules.yaml` until the one-week pilot shows acceptable
false-urgency rates; then consider `mode: auto`.

## 8. Cost guards checklist

- [ ] Cloud billing budget + alert configured.
- [ ] `gemini.max_calls_per_run` / `max_calls_per_day` set; Gemini project quota set in AI Studio/console.
- [ ] Tavily `daily_credit_cap` / `monthly_credit_cap` below your plan's allowance (or Tavily disabled).
- [ ] `max_alerts_per_run` / `max_alerts_per_day` set.
- [ ] Supabase usage alerts enabled; retention (`retention.articles_days`) set.
- [ ] Artifact Registry cleanup policy for old images.
