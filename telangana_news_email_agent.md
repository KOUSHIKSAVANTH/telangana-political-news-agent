# Telangana News Intelligence Agent — Email-Only MVP

**Version:** 1.0  
**Date:** 19 November 2026  
**Audience:** Communications team and AI coding agents (Codex / Claude Code)  
**Status:** Proposed implementation specification

## 1. Project goal

Build a cloud-hosted, automated Telangana news-monitoring agent that continues to operate when the developer's computer is turned off. The service discovers news from English and Telugu sources, filters and groups related coverage, uses Gemini to produce grounded summaries, and emails:

1. A **daily briefing at 9:00 AM Asia/Kolkata**.
2. **Breaking-news alerts** when a sufficiently important, credible development meets configured rules.

**Out of scope for MVP:** dashboard, public website, chatbot interface, n8n, custom model training, social posting, auto-generating public statements, and unrestricted scraping.

## 2. Chosen technology

| Component | MVP choice | Function |
|---|---|---|
| News discovery | Permitted publisher/official RSS or Atom feeds | Primary low-cost collection |
| Supplemental news search | Tavily Search API | Fill coverage gaps and find corroborating reports |
| AI model | Existing Gemini API key | Telugu-to-English translation, structured extraction, classification and summaries |
| Processing application | Python | Polling, normalization, deduplication, scoring, orchestration |
| Persistent storage | Supabase Postgres (Free plan initially) | Articles, stories, source citations, notification history |
| Email delivery | Gmail API with Google OAuth | Send internal-team messages |
| Execution | Google Cloud Run Jobs | Run Python tasks without a PC |
| Scheduling | Google Cloud Scheduler | Trigger monitoring and digest jobs |
| Secrets | Google Secret Manager / suitable protected environment secrets | Protect credentials |
| Logs | Cloud Logging | Error investigation and monitoring |

These are independent services. **Cloud Scheduler does not execute Python**; it invokes the Cloud Run Job. A coding agent can build and deploy the pipeline without n8n.

## 3. High-level flow

```text
Cloud Scheduler (every 15 minutes, Asia/Kolkata)
    -> Cloud Run Job: monitor
        -> RSS/Atom feeds (primary)
        -> Tavily (limited, targeted queries)
        -> normalize, save, deduplicate, cluster stories
        -> verify source/timestamp; Gemini structured analysis
        -> apply alert rules + cooldown
        -> Gmail API urgent email (only if criteria met)
        -> record delivery result in Supabase

Cloud Scheduler (daily at 09:00 Asia/Kolkata)
    -> Cloud Run Job: digest
        -> query recent, ranked stories from Supabase
        -> Gemini generates cited digest from stored evidence
        -> Gmail API sends to configured recipient list
        -> record delivery result
```

**Availability:** Cloud execution works with the developer's desktop switched off. “24/7 monitoring” means recurring automated checks; it **does not** guarantee instant coverage or 100% uptime. RSS publication, Tavily indexing, job execution, provider availability and email delivery introduce latency.

## 4. News coverage

### Topics

- Telangana state government, cabinet, legislature, departments and policy announcements.
- Political parties, elected officials and major political developments.
- Hyderabad and district-level civic issues, infrastructure, transport, power, water and public services.
- Weather, public safety and emergency notifications.
- Economy, investment, industry and employment.
- Education, healthcare, culture and notable community developments.

### Source policy

Start with a curated, verified list of sources in `sources.yaml`. Include a balanced mix of Telugu and English publishers and official sources. Potential publishers to investigate include Eenadu, Sakshi, V6 Velugu, Andhra Jyothy, Namasthe Telangana, Telangana Today, The Hindu, Deccan Chronicle, The Hans India and Siasat. Government press releases and agency notices should be prioritized for primary-source verification.

**Do not assume any listed publisher has a publicly accessible RSS feed.** Discover and test actual RSS/Atom URLs, terms, rate limits, timestamps, coverage and permissions before enabling collection. RSS is a standard feed format, usually available without a fee or API key, but publishers control availability and reuse conditions.

Do not bypass paywalls, login gates, robots controls or access restrictions. Store short excerpts and metadata as allowed, with canonical URLs; do not republish article bodies unnecessarily. Avoid treating a social post or AI-generated search snippet as independently verified reporting.

## 5. Collection policy and Tavily budget

### RSS first

- Poll enabled feeds every 15 minutes.
- Use HTTP conditional requests (ETag / Last-Modified) where supported.
- Handle malformed feeds and transient errors without crashing the full batch.
- Preserve original language, canonical URL, original publisher and publication time.
- Record ingestion timestamp separately from publisher timestamp.

### Tavily second

Use Tavily for a **small number of scheduled focused searches** (e.g., morning/afternoon), when important RSS coverage is missing, and for corroboration of high-priority stories. Search both Telugu and English phrases when useful. Store returned URLs and fetch only content permitted by the publisher. Cache queries/results and enforce daily/monthly credit budgets.

**Budget example:** One basic-credit search every 15 minutes would be ~2,880 searches in a 30-day month, which can exceed a 1,000-credit free allowance. Prefer RSS polling to repeated paid searching. Verify Tavily's live quota, credit consumption and plan before deployment.

## 6. Processing and Gemini behavior

### Processing stages

1. Parse and normalize article title, publisher, URL, language, source publication time and excerpt.
2. Canonicalize URLs, hash exact duplicates, and cluster semantically related articles into one story.
3. Detect Telangana relevance and tag geography (state, Hyderabad, district) and topic.
4. Translate Telugu passages into English when necessary while preserving names, titles, numbers and uncertainty.
5. Generate short summaries grounded **only** in the collected evidence.
6. Assign urgency and source confidence separately.
7. Check against earlier sent alerts to avoid repeats; trigger follow-ups only for material new facts.

### Required structured output

Have Gemini return validated JSON (use a schema, not freeform parsing):

```json
{
  "story_title": "string",
  "summary": "string",
  "why_it_matters_for_comms": "string",
  "language": "te|en|other",
  "districts": [],
  "topics": [],
  "priority": "critical|high|normal|low",
  "verification_status": "confirmed|developing|unverified",
  "evidence_url_ids": [],
  "material_update": false,
  "reason": "string"
}
```

Never let Gemini invent citation URLs, quotes, dates or factual details. All claims included in emails must be traceable to source records. AI confidence is **not** evidence of verification; apply deterministic checks and, for sensitive cases, human review.

## 7. Alert policy

| Priority | Handling |
|---|---|
| Critical | Alert quickly **after** checking an authoritative primary source or strong independent corroboration |
| High | Alert if comms-relevant and credible; otherwise flag for review or digest |
| Normal / Low | Digest only |
| Unverified / contested / politically sensitive claim | No automatic breaking alert; mark as developing or route to a human approver |

- Require a minimum evidence threshold defined in configuration; an official release can be a primary source for what that authority announced, but is not automatic proof of every claim it makes.
- Do not call a story “confirmed” merely because many outlets copy the same wire report.
- Deduplicate by story cluster; use a configurable 4–6 hour alert cooldown except for material factual developments.
- Send no empty or repetitive breaking alerts.
- Include `Published`, `Detected`, `Last checked`, and `Status` in alerts to avoid confusing older news with breaking news.
- Make thresholds and keywords configurable without code changes.

## 8. Email templates

### Breaking alert

**Subject:** `[TELANGANA ALERT][CRITICAL] <verified concise headline>`

Include:
- What happened (2–4 sentences).
- Why it matters to the communications team.
- Place/district and event time if known.
- Verification status and any uncertainty.
- 1–3 source links with publisher and publication timestamps.
- Suggested internal next step (monitor, escalate, prepare response), not a definitive public claim.
- Unique story/alert ID so revisions can be tracked.

### Daily briefing (9:00 AM IST)

**Subject:** `Telangana News Brief | YYYY-MM-DD`

Include:
- 5–10 top developments with linked sources.
- Sections: Government & Politics; Public Issues; Business & Economy; District Updates; Emerging Stories.
- Important follow-ups to previously alerted stories.
- Window covered, e.g. previous 24 hours in IST.
- Explicit note when few credible developments are found; never manufacture filler.

Default email language: English, retaining Telugu names accurately. Optional Telugu headlines can be added later. Send the same curated briefing to an authorized internal distribution list.

## 9. Data model (Supabase)

Suggested tables:

- `sources`: `id`, `name`, `language`, `feed_url`, `website`, `type`, `enabled`, `poll_interval`, `last_poll_at`, `last_error`.
- `articles`: `id`, `source_id`, `canonical_url UNIQUE`, `title`, `excerpt`, `language`, `published_at`, `discovered_at`, `content_hash`, `raw_metadata JSONB`.
- `stories`: `id`, `normalized_title`, `summary`, `category`, `districts JSONB`, `priority`, `verification_status`, `last_material_update_at`, `created_at`, `updated_at`.
- `story_articles`: `story_id`, `article_id`, `role` with unique pair.
- `runs`: `id`, `job_type`, `started_at`, `ended_at`, `status`, `items_seen`, `items_added`, `error_summary`.
- `notifications`: `id`, `story_id NULL`, `type`, `recipient_group`, `subject`, `idempotency_key UNIQUE`, `status`, `sent_at`, `provider_message_id`, `error_summary`.
- `settings`: `key`, `value` for reviewable non-secret configuration.

Design indexes around `published_at`, `canonical_url`, `story_id`, `priority`, and `notifications.idempotency_key`. Apply retention limits. Do not expose Supabase service-role credentials to browsers or public clients.

## 10. Credentials and setup checklist

### Required for this chosen stack

1. **Gemini API key** — already available; set `GEMINI_API_KEY` in secret storage. Select an appropriate model based on current pricing, throughput and quality.
2. **Google Cloud project + billing enabled** — configure Cloud Run Jobs and Cloud Scheduler APIs, service accounts, roles and schedules. A billing-enabled account may be required even if usage stays within free allowances.
3. **Supabase project** — create database and obtain `SUPABASE_URL` and a suitable server-side database credential (`SUPABASE_SERVICE_ROLE_KEY` if needed); use least privilege where possible.
4. **Gmail API OAuth credentials** — enable the Gmail API and configure a compliant sender authorization flow. For a Google Workspace organization, an admin-approved service account with domain-wide delegation may be possible; otherwise use an authorized OAuth refresh-token flow. **An API key alone does not authorize sending Gmail messages.**
5. **Recipient list** — approved comms team distribution emails, sender address, and owner for alert review.

### Optional

6. **Tavily API key** — `TAVILY_API_KEY`. Recommended for additional discovery and corroboration, but the first RSS-only prototype can run without it.

Never commit credentials or tokens to Git. Restrict IAM permissions, rotate secrets, and use a dedicated sending identity. Ask company IT for approval before using a company mailbox or distribution list.

## 11. Deployment details

Build a Python app with two explicit entrypoints:

- `python -m app.monitor` — one bounded collection/processing/alert run.
- `python -m app.digest` — one digest compilation/send run.

Package as a container image and create two Cloud Run Jobs (or one parameterized job with different commands). Create Cloud Scheduler jobs that securely invoke them with appropriate service-account IAM.

- **Monitor:** `*/15 * * * *` in `Asia/Kolkata`.
- **Digest:** `0 9 * * *` in `Asia/Kolkata`.

Use UTC for database timestamps; convert to IST in emails. Make jobs idempotent: a retry must not resend the same email or duplicate records. Use retry/backoff, max execution times and alerting on failed runs. Add a dry-run mode and a test-recipient allowlist before the first live send.

## 12. Cost model and safeguards

| Service | Budget assumption | Important qualification |
|---|---|---|
| Cloud Scheduler | Up to three jobs generally free per billing account; additional jobs billed | Verify current Google Cloud pricing and region/billing conditions |
| Cloud Run Jobs | May remain within free monthly compute allowance at modest usage | Image storage, network, CPU/memory and logs may produce charges |
| RSS | Usually free to read | Feed availability, publisher terms and rate limits vary |
| Supabase | Free plan usable initially | Storage, inactivity policies, quotas and upgrades apply |
| Gmail API | Generally no separate per-call charge within standard quotas | Gmail/Workspace sending limits, OAuth requirements and policy restrictions apply |
| Gemini API | Existing key | Free-tier eligibility/model quotas or paid token charges vary |
| Tavily | Optional free credits plus usage-based paid plans | Monitor exact credit consumption and prevent uncontrolled polling |

**Do not promise ₹0 forever.** Configure a Google Cloud budget alert (note: it does not cap spending automatically), Tavily hard query/credit limits in code, Gemini usage quotas where supported, database usage alerts and email rate limits. Estimated monthly costs should be recalculated using the current provider pricing pages before launch.

## 13. Suggested Python repository layout

```text
telangana-news-agent/
├── README.md
├── .env.example                 # variable names only, no secrets
├── pyproject.toml
├── Dockerfile
├── config/
│   ├── sources.yaml
│   ├── topics.yaml
│   └── alert_rules.yaml
├── app/
│   ├── monitor.py
│   ├── digest.py
│   ├── rss_client.py
│   ├── tavily_client.py
│   ├── dedupe.py
│   ├── cluster.py
│   ├── gemini_processor.py
│   ├── verification.py
│   ├── database.py
│   ├── email_sender.py
│   ├── templates/
│   │   ├── breaking.html
│   │   └── digest.html
│   └── settings.py
├── migrations/
├── tests/
│   ├── test_deduplication.py
│   ├── test_alert_rules.py
│   ├── test_idempotency.py
│   └── test_feed_parsing.py
└── deployment/
    └── cloud_run_scheduler.md
```

## 14. Delivery phases

**Phase 1 — Local proof of concept**
- Validate 5–10 permitted feeds (English + Telugu).
- Collect article metadata and original links.
- Test Gemini structured summaries on a manually reviewed sample.
- Produce a preview daily email with no external send.

**Phase 2 — Persistence and delivery**
- Add Supabase migrations, duplicate/story clustering and email logs.
- Configure Gmail OAuth and a dedicated test recipient.
- Send a test daily briefing and inspect source grounding and Telugu name accuracy.

**Phase 3 — Cloud automation**
- Deploy both tasks to Cloud Run Jobs.
- Configure Cloud Scheduler at 15-minute and 9 AM IST frequencies.
- Add credentials in secret storage, health checks, retries, quotas and alarms.

**Phase 4 — Breaking news and refinement**
- Add cautious alert policy, manual review path, and alert cooldowns.
- Add targeted Tavily searches only where RSS coverage is insufficient.
- Evaluate relevance, missed events, duplicate alerts, false urgency and costs over at least a one-week pilot.

## 15. Acceptance criteria

- [ ] Runs without local computer being on.
- [ ] Monitor job is automatically invoked every 15 minutes (subject to platform scheduling delays).
- [ ] Daily digest arrives near 9:00 AM IST and records delivery result.
- [ ] Every news item includes original source URL and publisher timestamp when available.
- [ ] Telugu stories are handled and proper names are preserved.
- [ ] Same article does not produce repeated emails; retries are idempotent.
- [ ] One developing event is clustered across outlets rather than emailed repeatedly.
- [ ] Unverified politically sensitive content cannot trigger autonomous urgent mail.
- [ ] Outages and authentication failures are logged and visible to an operator.
- [ ] API search volume, spend and email frequency are bounded.
- [ ] Secrets are never exposed in logs or source control.
- [ ] Team approves a test digest before production rollout.

## 16. Open decisions to confirm before deployment

1. Final email recipients and authorized sender mailbox / Google Workspace approval.
2. Required news focus: all Telangana news or primarily politics/government.
3. Whether breaking alerts must always have human approval or can auto-send after strict verification.
4. Approved source list, RSS availability and legal permission to use metadata/excerpts.
5. Gemini model and project quota; Tavily credit ceiling.
6. Preferred daily coverage window and whether weekends use the same schedule.
7. Retention policy and escalation owner for failures or inaccurate alerts.

## 17. Starter instruction for a coding agent

> Implement the email-only Telangana News Intelligence Agent according to this specification. Start with a local RSS-only dry-run proof of concept. Use Python, typed models, schema-validated Gemini results, Supabase migrations, Gmail OAuth, unit tests, and two containerized entrypoints for Cloud Run Jobs. Do not add n8n, a website, or a dashboard. Never invent source URLs or credentials. Discover and test valid publisher feeds; respect access terms. Do not send real emails until dry-run output is reviewed. Add Tavily as an optional budget-capped feature. Prepare a deployment guide for Google Cloud Scheduler/Cloud Run using Asia/Kolkata schedules and least-privilege IAM.

---

**Authoring note:** Pricing, free tiers, provider quotas and access policies change. Confirm them in official documentation when configuring billing and production credentials.
