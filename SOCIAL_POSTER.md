# Social Poster Change Log

## 2026-03-21
- Created the Django project `social_poster` and app `scheduler`.
- Added models for Meta credentials, social accounts, publishing targets, post logs, and Telegram report logs.
- Built a dashboard for Meta token input, synced FB/IG listing, connected vs unconnected grouping, and quick actions.
- Built target detail settings for Google Drive folder assignment, posts per day, posting window, and activation state.
- Added service modules for Meta sync, Google Drive access, Meta publishing, and Telegram reporting.
- Added management commands for sync, due posting, daily reporting, and scheduler polling.
- Added `.env.example`, `requirements.txt`, `AGENT.md`, and this change log file.
- Generated and stored a new `DJANGO_SECRET_KEY` in `.env`.
- Hardened Meta sync with fallback page-fetch endpoints and clearer token/permission error reporting in the dashboard.
- Added permanent credential deletion from the dashboard UI with a red delete button and DB removal.
- Added automatic `.env` loading in Django settings so Drive and Telegram credentials are available without manual shell export.
- Fixed scheduler media selection so it skips non-media files like `caption.txt`, reads caption text from `caption.txt`, and routes Facebook videos to the correct endpoint.
- Added target health summaries in the UI, recent log visibility, and a `Test Post Now` action on target detail pages.
- Hardened Instagram publishing by trying multiple public Google Drive media URLs before failing.
- Added a signed media proxy endpoint and `PUBLIC_APP_BASE_URL` support so Meta can fetch media through the app on a real public deployment instead of raw Google Drive links.
- Installed local ngrok agent under `tools/ngrok` and added PowerShell helper scripts for authtoken setup and tunnel startup with automatic `.env` update.
- Added Instagram video/reel publishing support with public proxy URLs and async container status polling before `media_publish`.
- Added clearer FB/IG rejection diagnostics with file metadata and probable-cause hints in post logs and the target detail UI.
- Standardized Instagram image proxy output to optimized JPEG and changed scheduler posting logic so FB/IG retries are tracked per platform instead of one platform consuming the other platform's daily slots.
- Changed mixed-media queue behavior so Facebook and Instagram stay on the same current file and do not advance to the next file until both platforms have succeeded for that file.
- Added a persistent cached public media pipeline so Drive files are materialized into stable app-served assets before posting to Facebook or Instagram.
- Added exact per-post daily timing support so each target can store one explicit time for every post count selected in the UI.
- Redesigned Telegram daily reports into a cleaner summary/activity/attention format and added `send_daily_report --date YYYY-MM-DD` for accurate same-day testing.
- Added `.gitignore` to prevent local secrets, virtualenv files, cached media, database, and service account credentials from being committed before GitHub push.

## Notes
- Current Instagram flow is MVP-level and supports image publishing only.
- Google Drive and Telegram credentials must be provided through environment variables.
