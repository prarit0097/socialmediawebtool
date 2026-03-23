# Social Media Web Tool

A Django-based web application for automated posting from Google Drive to Facebook Pages and Instagram accounts.

## What This Project Does
- accepts a Meta access token from the web UI
- syncs connected Facebook Pages and Instagram accounts
- lets you map a Google Drive folder to each posting target
- supports daily posting frequency with exact per-post timings
- attempts to publish the same media to Facebook and Instagram
- sends a Telegram report with posting status summaries

## Main Features
- Meta credential management from the dashboard
- connected vs unconnected FB/IG target grouping
- Google Drive folder mapping per target
- caption support through `caption.txt` or a default caption
- `jpeg`, `jpg`, `png`, and `mp4` media handling
- per-target `Test Post Now` action
- target health view and recent post logs
- daily scheduler and Telegram reporting commands
- local ngrok helper scripts for public URL testing
- optional OpenAI-compatible AI layer for captions, hashtags, rewrites, translations, duplicate warnings, classifications, suggestions, and AI report summaries

## Tech Stack
- Python 3.10+
- Django 5.1
- Requests
- Google Drive API
- Meta Graph API
- Pillow

## Project Structure
```text
social_poster/            Django project settings and URLs
scheduler/                Main app: models, views, services, templates
scripts/                  Helper PowerShell scripts
tools/ngrok/              Local ngrok binary for public testing
AGENT.md                  Repository-level working rules
SOCIAL_POSTER.md          Beginner-friendly project explanation and updates
requirements.txt          Python dependencies
.env.example              Environment variable template
```

## Setup
```powershell
cd "E:\social media web tool"

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt

Copy-Item .env.example .env
python manage.py migrate
python manage.py runserver
```

## Required Environment Variables
Set these in `.env`:

```env
DJANGO_SECRET_KEY=change-me
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=*
DJANGO_CSRF_TRUSTED_ORIGINS=
APP_TIME_ZONE=Asia/Kolkata
META_GRAPH_BASE_URL=https://graph.facebook.com/v22.0
GOOGLE_SERVICE_ACCOUNT_FILE=E:\path\to\service-account.json
GOOGLE_SERVICE_ACCOUNT_EMAIL=service-account@project.iam.gserviceaccount.com
PUBLIC_APP_BASE_URL=https://your-public-domain.example.com
TELEGRAM_BOT_TOKEN=123456:abc
TELEGRAM_CHAT_ID=123456789
REPORT_HOUR=9
SCHEDULER_POLL_SECONDS=60
SCHEDULER_CATCHUP_MINUTES=60
INSTAGRAM_CONTAINER_POLL_SECONDS=5
INSTAGRAM_CONTAINER_MAX_POLLS=24
AI_API_KEY=PASTE_YOUR_OPENAI_KEY_HERE
AI_API_BASE_URL=https://api.openai.com/v1
AI_MODEL=openai/gpt-4.1-nano
AI_FALLBACK_MODEL=openai/gpt-4.1-mini
AI_TIMEOUT_SECONDS=90
```

For live HTTPS domains, the app automatically trusts `PUBLIC_APP_BASE_URL` for CSRF. If you need more trusted origins, set `DJANGO_CSRF_TRUSTED_ORIGINS` as a comma-separated list.

## AI Features
If you configure `AI_API_KEY`, the app can:
- generate captions and hashtags for the next media file
- create short, long, Hindi, English, and Hinglish rewrites
- classify content by category and tags
- warn about likely duplicates or low-quality/spammy media context
- suggest better posting times from past success logs
- enhance the Telegram daily report with an AI summary

These controls are available on each target detail page under `AI Settings` and `AI Workspace`.

Default AI model order in this repo:
- primary: `openai/gpt-4.1-nano`
- fallback: `openai/gpt-4.1-mini`

## Google Drive Requirements
- share the Drive folder with `GOOGLE_SERVICE_ACCOUNT_EMAIL`
- keep media files in supported formats:
  - `.jpeg`, `.jpg`
  - `.png`
  - `.mp4`
- optionally add `caption.txt` for caption text

## Main Commands
Run Django server:

```powershell
.\.venv\Scripts\Activate.ps1
python manage.py runserver
```

Run the scheduler loop:

```powershell
.\.venv\Scripts\Activate.ps1
python manage.py run_scheduler
```

Run due posts manually:

```powershell
.\.venv\Scripts\python.exe manage.py run_due_posts
```

Send a Telegram report manually:

```powershell
.\.venv\Scripts\python.exe manage.py send_daily_report --date 2026-03-21 --force
```

Run tests:

```powershell
.\.venv\Scripts\python.exe manage.py test
```

## ngrok Testing
Configure ngrok auth token:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_ngrok.ps1 -AuthToken "YOUR_NGROK_TOKEN"
```

Start a tunnel and update `.env` automatically:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_ngrok_tunnel.ps1 -Port 8000
```

Then restart Django so `PUBLIC_APP_BASE_URL` is reloaded.

## Notes
- Facebook posting is more reliable than Instagram in local tunnel-based testing.
- Instagram media acceptance still depends on Meta-side validation and media compatibility.
- `.env`, service account credentials, local DB, cache files, and virtualenv are ignored by Git.

## Repository Rules
- after every meaningful change, update `SOCIAL_POSTER.md`
- after every meaningful change, push the latest code to GitHub
