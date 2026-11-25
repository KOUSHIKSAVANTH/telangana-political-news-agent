# Telangana Political News Agent

A news monitoring agent for **political communications teams in Telangana**. It reads English
and Telugu news, works out which reports describe the same event, checks how well each story is
corroborated, and sends the team, by **email** and/or **Telegram**:

- **A daily brief at 09:00 IST**: the most important political and governance developments of the
  last 24 hours, grouped by section, with source links.
- **Cautious breaking alerts**: only for high-priority stories that are recent and backed by
  independent sources. By default every alert goes to a human reviewer first.

There is no website or dashboard. The output is email and, optionally, a send-only Telegram bot.

> The code lives in [`telangana-news-agent/`](telangana-news-agent/). Run all commands below from
> inside that folder. The original specification is
> [`telangana_news_email_agent.md`](telangana_news_email_agent.md).

---

## Contents

1. [Goal and scope](#1-goal-and-scope)
2. [What the emails look like](#2-what-the-emails-look-like)
3. [How it works](#3-how-it-works)
4. [Requirements](#4-requirements)
5. [Installation](#5-installation)
6. [Configuration (`.env`)](#6-configuration-env)
7. [Running it](#7-running-it)
8. [Setting up the database (Supabase)](#8-setting-up-the-database-supabase)
9. [Setting up email sending (Gmail)](#9-setting-up-email-sending-gmail)
10. [Telegram delivery (optional, free)](#10-telegram-delivery-optional-free)
11. [Running it on a schedule](#11-running-it-on-a-schedule)
12. [Customising sources, keywords and rules](#12-customising-sources-keywords-and-rules)
13. [Safety and reliability](#13-safety-and-reliability)
14. [Costs and quotas](#14-costs-and-quotas)
15. [Troubleshooting](#15-troubleshooting)
16. [Project layout](#16-project-layout)
17. [Development and tests](#17-development-and-tests)
18. [Known limitations and status](#18-known-limitations-and-status)

---

## 1. Goal and scope

**Goal:** give a political communications team one reliable morning briefing and early warning of
major Telangana developments, without anyone having to read a dozen news sites in two languages.

**Scope: political content only.** That means politics, government, policy, elections, the
legislature, courts on public matters, public administration, and public issues where government
action is at stake (protests, disasters, civic failures, welfare schemes, farmers, jobs).

**Out of scope:** entertainment, cinema, sports, lifestyle, astrology, celebrity and gadget news.
These are dropped in two stages:

1. **When news is collected:** publisher section tags (e.g. "Sport", "Entertainment"), URL sections
   and keywords. Nothing is sent to the AI, so no AI calls are spent. An item is kept anyway if it
   also mentions politics, so "Actor joins BJP" and "Government order on cinema ticket prices" stay in.
2. **When stories are analysed:** the AI decides whether each story is political. Non-political
   stories never reach alerts or the brief.

**Requirements this project was built to meet:**

| Requirement | How it's met |
|---|---|
| English **and Telugu** sources | 13 verified feeds (8 English, 4 Telugu, 1 official). Telugu headlines are translated so stories can be grouped across languages. |
| No made-up facts or links | The AI sees only stored article text. Its cited article IDs are checked against the input. Links in emails come only from stored records. |
| Don't raise false alarms | Verification is done by code, not the AI: independent outlets are counted, copied wire reports count once, and an official source counts. Unverified stories never trigger alerts. |
| Never send the same email twice | Every email claims a unique key before it is sent, so retries and re-runs are safe. |
| Bounded cost | Hard caps on AI calls per run and per day, on search credits, and on alerts per run and per day. |
| Safe by default | Dry-run is the default, alerts go to a reviewer, and live sends are allowed only to an allowlist. |
| Runs without a computer switched on | GitHub Actions (free) or Google Cloud Run Jobs with Cloud Scheduler. |
| Editable without coding | Sources, keywords, districts, thresholds and sections are YAML files in `config/`. |

---

## 2. What the emails look like

Real emails are formatted HTML with clickable links. The summaries below are illustrative.

**Daily brief.** Subject: `Telangana News Brief | 2026-11-20`

```
TELANGANA NEWS BRIEF | 2026-11-20
Covering 19 Nov 2026, 09:00 IST - 20 Nov 2026, 09:00 IST
6 development(s) from 7 source(s).

== POLITICS & GOVERNMENT ==

- [HIGH] Opposition workers detained ahead of CM's district visit
  Police detained several opposition workers before the Chief Minister's
  visit, according to reports. Party leaders called it an attempt to stop
  protests; police said the measures were precautionary.
  Comms: Likely to be raised as curbing dissent; expect questions on policing.
  Status: confirmed | Siddipet
  * Publisher A (19 Nov 2026, 10:42 IST): <link>
  * Publisher B (19 Nov 2026, 11:05 IST): <link>

== PUBLIC ISSUES & GOVERNANCE ==
...
== EMERGING STORIES ==
...
Automated internal monitoring; verify before any public statement.
```

**Breaking alert.** Subject: `[REVIEW][TELANGANA ALERT][HIGH] <headline>`. It contains what happened,
why it matters, a suggested next step, the place, the publish, detect and check times, the
verification status (e.g. "confirmed: 3 independent sources"), and numbered source links. In review
mode, a banner says it has not been sent to the team.

Brief sections: **Politics & Government · Public Issues & Governance · Economy & Policy · District
Politics & Governance · Emerging Stories** (unverified or still-developing sensitive items). On a quiet
day the brief says so rather than adding filler.

---

## 3. How it works

```
RSS/Atom feeds (13) ─┐
Tavily search (optional) ┴─> clean + canonical URL ─> drop exact duplicates (URL / content hash)
   ─> Telangana relevance ─> political-scope filter (drop sports/cinema/lifestyle)
   ─> AI: translate Telugu headlines
   ─> group into stories (title similarity, 36-hour window)
   ─> AI: structured analysis (schema-validated JSON; cited IDs must exist)
   ─> verification by code (independent outlets, wire copies collapsed, official sources)
   ─> alert policy (priority, age, cooldown, caps, sensitive → reviewer) ─> Gmail API and/or Telegram
```

There are two commands:

| Command | What it does | Typical schedule |
|---|---|---|
| `python -m app.monitor` | Collects news, groups and analyses it, and sends any breaking alerts | every 15–60 min |
| `python -m app.digest` | Builds and sends the daily brief (once per IST date) | 09:00 IST |

**AI provider:** Groq or Google Gemini. Both use the same prompts and the same validation. With no AI
key, the agent still runs in a **keyword-only fallback**: it uses publishers' own headlines and
excerpts, writes no text of its own, and is very conservative about priority.

---

## 4. Requirements

### Software

| What | Needed for | Notes |
|---|---|---|
| **Python 3.12+** | everything | [python.org](https://www.python.org/downloads/). On Windows, tick "Add Python to PATH". |
| Git | cloning the repository | |
| Google Cloud CLI (`gcloud`) | cloud deployment only | [install guide](https://cloud.google.com/sdk/docs/install). On Windows: `winget install Google.CloudSDK` |

Docker is **not** needed: the cloud deployment builds the container image on Google's servers.

### Accounts and keys

| Account | Needed for | Cost |
|---|---|---|
| **Groq** *or* **Google AI Studio (Gemini)** | AI summaries, Telugu translation, priority | Free tiers available |
| Supabase | Database for cloud runs (locally, a SQLite file is used) | Free tier |
| Google account with Gmail | Sending emails | Free (Workspace account recommended) |
| Telegram *(optional)* | Alerts and brief in a Telegram group | Free |
| GitHub *or* Google Cloud | Scheduled runs | GitHub Actions free; Google Cloud needs billing enabled |
| Tavily *(optional)* | Extra web search | Free tier |

You can try everything locally with **just Python and one AI key**, or with no key at all.

---

## 5. Installation

```bash
git clone https://github.com/KOUSHIKSAVANTH/telangana-political-news-agent.git
cd telangana-political-news-agent/telangana-news-agent

python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -e ".[dev]"
```

> On Windows, if PowerShell blocks `Activate.ps1`, run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or call
> `.venv\Scripts\python` directly instead of `python`.

Check the installation (no keys needed):

```bash
pytest
```

Then create your settings file:

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

`.env` is git-ignored. **Never commit it.**

---

## 6. Configuration (`.env`)

Leave a line blank to switch that setting off. Paste values directly after `=` with no quotes or spaces.

### Needed to try it locally

| Key | What it is | Where to get it |
|---|---|---|
| `GROQ_API_KEY` | AI key (starts `gsk_`) | [console.groq.com/keys](https://console.groq.com/keys) |
| `DRY_RUN` | `true` saves emails as files in `out/` and sends nothing. **Keep `true` until you've reviewed the output.** | — |

### Optional

| Key | What it is |
|---|---|
| `LLM_PROVIDER` | Forces the AI provider: `groq`, `gemini` or `none`. Blank uses whichever key is set (Gemini first). |
| `GROQ_MODEL` | Overrides the Groq model (default `openai/gpt-oss-120b`). |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini instead of Groq: [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `TAVILY_API_KEY` | Extra search: [app.tavily.com](https://app.tavily.com). Also set `tavily.enabled: true` in `config/pipeline.yaml`. |
| `LOG_LEVEL` | `INFO` (default) or `DEBUG` |

### Needed for real emails and cloud runs

| Key | What it is |
|---|---|
| `DATABASE_URL` | Supabase **Session pooler** connection string ([section 8](#8-setting-up-the-database-supabase)). Blank uses `local.db`. |
| `EMAIL_SENDER` | Full address of the sending mailbox. It must be the account you authorise in [section 9](#9-setting-up-email-sending-gmail). |
| `EMAIL_TEAM_RECIPIENTS` | Comma-separated list. Receives the daily brief and approved alerts. |
| `EMAIL_REVIEWER_RECIPIENTS` | Comma-separated list. Receives alerts that need a human check (all alerts in review mode). |
| `EMAIL_RECIPIENT_ALLOWLIST` | Comma-separated list. **The agent refuses to email anyone not on it.** Include every team and reviewer address. |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` | Gmail permission (Option A, recommended). |
| `DELIVERY_CHANNELS` | `email` (default), `telegram`, or `email,telegram` |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather ([section 10](#10-telegram-delivery-optional-free)) |
| `TELEGRAM_TEAM_CHAT_IDS`, `TELEGRAM_REVIEWER_CHAT_IDS` | Comma-separated chat IDs. Messages only go to these chats. |
| `GMAIL_SERVICE_ACCOUNT_JSON`, `GMAIL_DELEGATED_USER` | Option B, Workspace domain-wide delegation (needs an admin). Leave blank if you use Option A. |

---

## 7. Running it

```bash
# 1. Check the AI key: lists models, confirms the configured one, runs a test Telugu translation
python -m app.groq_processor

# 2. One monitoring cycle: collect, filter, group, analyse, evaluate alerts
python -m app.monitor

# 3. Build today's brief
python -m app.digest
python -m app.digest --force     # rebuild even if today's brief was already produced
```

With `DRY_RUN=true`, emails are written to `out/` as `.html` (open it in a browser) and `.txt`.

**Recommended first run:** do steps 1–3 in dry-run, read `out/dryrun_digest_<date>.html`, and check the
Telugu names, summaries and priorities before you turn on real sending.

**Turning on real emails:**
1. Put only your own address in the recipient and allowlist settings.
2. Set `DRY_RUN=false`.
3. Run `python -m app.digest --force` and check your inbox, including the spam folder.

Only then add the team's addresses.

> **AI quota tip:** the first run on an empty database finds hundreds of stories. AI calls are capped
> (Groq: 15 per run, 400 per day by default; see `config/pipeline.yaml`), so it takes a few runs to
> catch up. Stories wait in a queue; they are never dropped.

### Database utilities

```bash
python -m app.database --init       # create tables in DATABASE_URL (for Supabase, prefer migrations/001_init.sql, which also enables RLS)
python -m app.database --emit-sql   # print the Postgres schema (used to generate migrations/)
```

---

## 8. Setting up the database (Supabase)

Locally the agent uses `local.db` (SQLite) automatically. Cloud runs need a hosted database because
each run starts on a fresh machine.

1. At [supabase.com](https://supabase.com), create a **New project**. A region near your users is
   best (e.g. Mumbai). Save the database password.
2. Open the project → **Connect** (top bar) → **Direct / Connection string** → set the **Method**
   dropdown to **Session pooler**.
3. Copy the string. It looks like
   `postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`.
4. Replace `[YOUR-PASSWORD]` (brackets included) and paste it as `DATABASE_URL` in `.env`.
5. Create the tables: paste `migrations/001_init.sql` into Supabase's **SQL Editor** and click
   **Run**. Prefer this to `python -m app.database --init`, which creates the tables but does not turn
   on Row-Level Security.

> - **Use the Session pooler, not "Direct connection".** The direct host is IPv6-only and fails on most
>   networks and on Cloud Run with `failed to resolve host`.
> - **Don't use the `https://<ref>.supabase.co` URL** or the publishable/anon keys. Those are for web
>   apps.
> - **Passwords containing `@ : / ? # %`** must be URL-encoded. It's simpler to use letters and digits.

The migration enables Row-Level Security on every table, which blocks Supabase's public web API from
reading them. The agent connects as the database owner and is unaffected.

---

## 9. Setting up email sending (Gmail)

The agent sends through the Gmail API with a **send-only** permission: it cannot read the mailbox.

1. At [console.cloud.google.com](https://console.cloud.google.com), create or select a project.
2. **APIs & Services → Library**: search for **Gmail API** and click **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - **Internal** if the sender is a Google Workspace (company) account. Only accounts in your
     organisation can *authorise* the app, but the emails can go to **any** recipient.
   - **External** for a personal Gmail. Add the sender as a test user. In "Testing" status the
     permission expires after 7 days, so publish the app for long-term use.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app**. Download
   the JSON file.
5. Run:
   ```bash
   python scripts/gmail_auth.py "<path-to>/client_secret_XXXX.json"
   ```
   A browser opens. **Sign in as the sending mailbox** and click Allow. The script prints
   `GMAIL_CLIENT_ID` and `GMAIL_REFRESH_TOKEN`; put both in `.env`. Copy `GMAIL_CLIENT_SECRET` from the
   JSON's `"client_secret"` field (it starts with `GOCSPX-`).
6. Delete the JSON file, or keep it somewhere private. `client_secret*.json` is git-ignored.

If the sender is a company mailbox, check with IT before connecting an app to it.

---

## 10. Telegram delivery (optional, free)

Alerts and the brief can also go to a **Telegram group or channel**, which gives instant phone
notifications. Use email, Telegram, or both: `DELIVERY_CHANNELS=email,telegram`.

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts. Copy the **bot token**
   into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Create a **private group** (or channel) for the team and add the bot. For a channel, make the bot
   an **admin** with permission to post. Optionally create a second group for the reviewer.
3. Send any message in the group, then run:
   ```bash
   python scripts/telegram_setup.py
   ```
   It checks the token and lists the chats the bot has seen, with their IDs. Group and channel IDs
   are negative, e.g. `-1001234567890`.
4. Put the IDs in `.env`: `TELEGRAM_TEAM_CHAT_IDS` (brief and approved alerts) and
   `TELEGRAM_REVIEWER_CHAT_IDS` (alerts needing review). Set `DELIVERY_CHANNELS=email,telegram` or
   `telegram`.
5. Test: `python scripts/telegram_setup.py --test <chat-id>`.

How it behaves:
- **Format:** alerts are one compact message (headline, status, summary, next step, source links).
  Long briefs are split into several messages at story boundaries, because of Telegram's
  4,096-character limit.
- **Same safety rules as email:** dry-run writes `out/*.telegram.txt` instead of sending, messages
  go only to the configured chat IDs, and each message is sent once (separate once-only key per
  channel). An alert sent on both channels counts as **one** alert for cooldowns and caps.
- **Send-only:** the bot never reads or replies to messages.
- **Privacy:** Telegram bot and group messages are stored on Telegram's servers and are **not
  end-to-end encrypted**. Use a private group, limit who can add members, and decide whether that
  suits your team's internal notes.

---

## 11. Running it on a schedule

Pick **one** option. Both run the monitor and the 09:00 IST brief without your computer.

### Option A: GitHub Actions (free, no card)

The workflow [`.github/workflows/news-agent.yml`](.github/workflows/news-agent.yml) runs the
**monitor every hour** and the **brief at 09:00 IST** on GitHub's servers.

1. Set up Supabase ([section 8](#8-setting-up-the-database-supabase)). It's **required**, because each
   run starts on a fresh machine with no local database.
2. On GitHub, open the repository → **Settings → Secrets and variables → Actions**.
3. **Secrets** tab → **New repository secret** for each value you use. Copy the values exactly as in
   your `.env`:
   `DATABASE_URL`, `GROQ_API_KEY` (or `GEMINI_API_KEY`), `EMAIL_SENDER`, `EMAIL_TEAM_RECIPIENTS`,
   `EMAIL_REVIEWER_RECIPIENTS`, `EMAIL_RECIPIENT_ALLOWLIST`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`,
   `GMAIL_REFRESH_TOKEN`, and if you use Telegram, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_TEAM_CHAT_IDS`,
   `TELEGRAM_REVIEWER_CHAT_IDS`. Optional: `TAVILY_API_KEY`.
4. **Variables** tab (not secret) → `DRY_RUN` = `true` to start, and `DELIVERY_CHANNELS` = `email`,
   `telegram` or `email,telegram`.
5. **Actions** tab → **news-agent** → **Run workflow** to test it by hand (choose `monitor` or
   `digest`). In dry-run on a private repository, the previews are attached to the run as a
   downloadable artifact.
6. When the previews look right, set the `DRY_RUN` variable to `false`.

Things to know:
- **Minutes:** private repositories get 2,000 free minutes a month. Hourly monitoring plus the daily
  brief uses roughly 1,000–1,500. Public repositories have no limit. Don't run it every 15 minutes
  on a private repository.
- **Timing:** GitHub may start scheduled runs a few minutes late, and **pauses schedules after 60 days
  without repository activity**. Re-enable them from the Actions tab.
- **Changing the schedule:** edit the `cron` lines in the workflow file. They are in UTC: `30 3 * * *`
  is 09:00 IST.
- **Feeds:** runs come from GitHub's servers (usually in the US). If a news site blocks them, it shows
  up as a failing source in the brief's footer.

### Option B: Google Cloud (Cloud Run Jobs + Cloud Scheduler)

Two **Cloud Run Jobs** (`news-monitor`, `news-digest`) share one container image. Two **Cloud
Scheduler** jobs start them on an `Asia/Kolkata` schedule. This option is better if you need
on-the-dot timing, runs every 15 minutes, or company infrastructure.

**Prerequisites:**
- A Google Cloud project **with billing enabled**. Cloud Run, Scheduler and Secret Manager can't be
  turned on without it, even if usage stays within free tiers. New accounts can use the free trial
  credit.
- The `gcloud` CLI, signed in: `gcloud init`.
- Supabase and Gmail (and/or Telegram) set up as above, and the dry-run output reviewed.

**Full step-by-step guide:** [`telangana-news-agent/deployment/cloud_run_scheduler.md`](telangana-news-agent/deployment/cloud_run_scheduler.md).
It covers enabling the services, least-privilege service accounts, storing keys in **Secret
Manager** (never in the image), building the image with Cloud Build, creating the jobs, the
schedules, failure alerts and going live. For Telegram, also add `TELEGRAM_BOT_TOKEN` and the chat
IDs as secrets, and `DELIVERY_CHANNELS` to the job settings.

**Choosing the monitor frequency:** the spec uses every 15 minutes. On a free AI tier, every 30–60
minutes uses far fewer AI calls; the daily caps apply either way.

### Going live (either option)

The schedules start with `DRY_RUN=true`. After the team approves a test brief, switch to
`DRY_RUN=false`. Keep `mode: review` in `config/alert_rules.yaml` until a pilot week shows acceptable
false-alarm rates.

---

## 12. Customising sources, keywords and rules

You don't need to touch any code: edit the YAML files and re-run (or rebuild the image for the cloud).

| File | What you can change |
|---|---|
| `config/sources.yaml` | News feeds. Each entry has an `id`, `name`, `language` (`en`/`te`), `type` (`publisher`/`official`), `feed_url`, `independence_group` (feeds from the same publisher share a group, so they count as one source), `telangana_specific` (if `false`, items must mention Telangana keywords), `enabled`. |
| `config/topics.yaml` | **`scope`**: what counts as non-political (`exclude_sections`, `exclude_keywords`) and the `political_keywords` that keep an item in scope. Also Telangana relevance keywords, the 33 districts (English + Telugu names), category keywords, priority keywords and politically sensitive keywords. |
| `config/alert_rules.yaml` | `mode` (`review`/`auto`), which priorities alert, minimum independent sources, official-source rule, maximum story age (6 h), cooldown (5 h), alert caps, blocking of sensitive stories from auto-send. |
| `config/pipeline.yaml` | AI model and call caps, temperature, Tavily settings and credit caps, clustering threshold, brief sections and size, data retention. |

**Adding a feed:** check that it is a real RSS/Atom feed (open it in a browser, or test it with
`feedparser`). Add an entry with a new `id` and set `enabled: true`. Confirm the publisher's terms
allow this use. Only headlines, short excerpts and links are stored.

**Keyword matching rules:** English keywords match whole words, ignoring case. Telugu keywords match
anywhere in the text, because Telugu attaches suffixes (e.g. తెలంగాణలో).

---

## 13. Safety and reliability

- **Grounded AI:** the model sees only stored evidence. Output must match a strict JSON schema, and any
  cited article ID not in the input is rejected. If the AI fails, the agent falls back to the
  publishers' own excerpts; it never invents text.
- **Verification by code:** the model's "confirmed / developing / unverified" label is capped by what
  the evidence supports. Copied wire text and multiple feeds from one publisher count as one source.
- **Conservative alerting:** no alerts for unverified stories. Sensitive or developing stories always
  go to the reviewer. Cooldowns and per-run and per-day caps limit email volume.
- **Idempotent delivery:** each email claims a unique key (`digest:<date>`,
  `alert:<story>:v<n>:<priority>`) before it is sent. Retried runs never send twice; only failed sends
  can be retried.
- **Safe defaults:** `DRY_RUN=true`, `mode: review`, and the recipient allowlist.
- **Secrets:** `.env` and `client_secret*.json` are git-ignored. In the cloud, keys live in Secret
  Manager. The Gmail permission is send-only.
- **Respectful collection:** conditional requests (ETag / Last-Modified) and a descriptive User-Agent.
  No paywall bypassing, and TLS verification is never disabled.
- **Logs:** JSON logs with severity in the cloud. The `runs` table records every execution; failing
  sources are listed in the brief's footer.

---

## 14. Costs and quotas

| Item | Default limit | Where to change |
|---|---|---|
| Groq calls | 15 per run, 400 per day | `config/pipeline.yaml` → `groq` |
| Gemini calls | 20 per run, 1200 per day | `config/pipeline.yaml` → `gemini` |
| Tavily credits | 20 per day, 600 per month (off by default) | `config/pipeline.yaml` → `tavily` |
| Alerts | 3 per run, 12 per day | `config/alert_rules.yaml` |
| Stored articles | 45 days | `config/pipeline.yaml` → `retention` |

GitHub Actions: about 1,000–1,500 of the 2,000 free monthly minutes for a private repository at
hourly monitoring. Telegram: free. Google Cloud at this scale (two small scheduled jobs) usually costs little or nothing, but **set a
billing budget alert**. It notifies you but doesn't cap spending. Check current free-tier limits for
Groq (console.groq.com/settings/limits), Gemini, Supabase and Cloud Run before going live.

---

## 15. Troubleshooting

| Problem | Fix |
|---|---|
| `gcloud` is not recognised after installing | Restart VS Code or the terminal completely, so it picks up the new PATH. |
| `failed to resolve host 'db.<ref>.supabase.co'` | You used the Direct connection. Use the **Session pooler** string. |
| Database errors with an `https://...supabase.co` URL | That's the web API URL, not a database string. See [section 8](#8-setting-up-the-database-supabase). |
| `No such file or directory: client_secret...json` | Pass the full path to the downloaded file (often in your Downloads folder). |
| Gmail `invalid_grant` | The refresh token expired or was revoked (common with External apps in "Testing" status). Re-run `scripts/gmail_auth.py`. |
| Email refused for a recipient | Add the address to `EMAIL_RECIPIENT_ALLOWLIST`. |
| Groq 429 / rate limit | Expected on free tiers. The agent waits and retries, then defers to the next run. Lower `max_calls_per_run` or run less often. |
| Groq model not available | Run `python -m app.groq_processor` to list your models, then set `GROQ_MODEL`. |
| `telangana.gov.in` TLS error | Some networks are served an invalid certificate for this site. The agent won't bypass TLS. It is reported in the brief's footer; retry from the cloud. |
| Brief says "already produced" | One brief per IST date. Use `python -m app.digest --force` to rebuild. |
| `telegram_setup.py` shows no chats | Send a message in the group *after* adding the bot, then re-run. For a channel, make the bot an admin. Telegram keeps these updates for 24 hours. |
| Telegram `chat not found` / `bot was kicked` | Wrong chat ID (include the minus sign), or the bot was removed from the group. |
| GitHub Actions fails at "Check database secret" | Add the `DATABASE_URL` secret. A hosted database is required on GitHub's servers. |
| Scheduled GitHub runs stopped | GitHub pauses schedules after 60 days without repository activity. Re-enable them in the Actions tab. |
| Telugu text garbled in a Windows console | Set `PYTHONIOENCODING=utf-8` (the email files are fine). |

---

## 16. Project layout

```
.github/workflows/news-agent.yml # free scheduler (GitHub Actions)
README.md                       # this file
telangana_news_email_agent.md   # original project specification
telangana-news-agent/           # the agent: run all commands from inside this folder
├── app/
│   ├── monitor.py          # entry point: collect → filter → cluster → analyse → alert
│   ├── digest.py           # entry point: daily brief
│   ├── pipeline.py         # shared analysis, AI selection/budget, idempotent delivery
│   ├── rss_client.py       # feed fetching (conditional GET), parsing, feed tags
│   ├── tavily_client.py    # optional search
│   ├── relevance.py        # Telangana relevance, political scope, districts, categories
│   ├── dedupe.py           # URL canonicalisation, text normalisation (incl. Telugu), hashing
│   ├── cluster.py          # grouping articles into stories
│   ├── verification.py     # independent-source counting, status capping
│   ├── alerts.py           # alert policy (pure function)
│   ├── gemini_processor.py # shared LLM prompts/validation + Gemini provider
│   ├── groq_processor.py   # Groq provider + key check (python -m app.groq_processor)
│   ├── email_sender.py     # dry-run files or Gmail API; allowlist enforcement
│   ├── telegram_sender.py  # Telegram Bot API (send-only); message splitting
│   ├── database.py         # SQLAlchemy schema/queries (SQLite locally, Postgres in prod)
│   ├── models.py           # typed models incl. the AI output schema
│   ├── settings.py         # .env + YAML loading
│   ├── logging_setup.py    # text logs locally, JSON in the cloud
│   └── templates/          # email (HTML + text) and Telegram templates
├── config/                 # sources, topics/scope, alert rules, pipeline settings
├── migrations/001_init.sql # Postgres schema (generated) with RLS enabled
├── deployment/             # Cloud Run Jobs + Cloud Scheduler guide
├── scripts/
│   ├── gmail_auth.py       # one-time Gmail permission helper
│   └── telegram_setup.py   # check bot token, find chat IDs, send a test message
├── tests/                  # pytest suite (no network or keys needed)
├── Dockerfile
├── pyproject.toml
└── .env.example            # copy to .env
```

---

## 17. Development and tests

```bash
pytest            # 89 tests; network, AI and Telegram calls are mocked
```

The tests cover:
- feed parsing and URL canonicalisation
- de-duplication and clustering (including Telugu)
- alert rules
- idempotent delivery
- the Groq and Gemini providers: schema mode and fallback, rate-limit retries, rejection of invented
  IDs, budgets
- political-scope filtering
- Telegram delivery: message splitting, dry-run, chat restrictions, rate limits, token redaction,
  and multi-channel delivery

After changing tables in `app/database.py`, regenerate the migration:

```bash
python -m app.database --emit-sql > migrations/001_init.sql
```

---

## 18. Known limitations and status

**Limitations:**
- Stories are grouped by headline similarity, so some wrong merges or splits remain. Grouping across
  languages depends on the AI translation of Telugu headlines.
- The keyword-only fallback (no AI key) can't judge priority well; use an AI key for useful alerts.
- Eenadu, Andhra Jyothy and New Indian Express have no working public feed at the moment, and the
  Indian Express Hyderabad feed is stale, so they're disabled. See `config/sources.yaml`.
- AI output quality, especially Telugu names, should be reviewed on a manual sample before going live.
- Confirm each publisher's reuse terms before production use.

**Status:**
- [x] Feeds validated; collection, filtering, grouping, AI analysis, verification, alert policy
- [x] Political-only scope filter
- [x] Groq and Gemini providers with budgets; keyword fallback
- [x] Dry-run previews; Supabase schema; Gmail sending; idempotent delivery
- [x] Telegram delivery (optional)
- [x] Free scheduling with GitHub Actions; container and Cloud Run / Scheduler guide
- [ ] Cloud deployment and a one-week pilot to tune alert thresholds
