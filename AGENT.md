# Global Instructions

- Maintain this repository as a Django web app for automated posting from Google Drive to separate Facebook pages and Instagram profiles.
- Update `SOCIAL_POSTER.md` after every meaningful change in the project.
- After every meaningful project change, update `SOCIAL_POSTER.md` and push the latest code to GitHub.
- Preserve these product requirements:
  - Meta access token input through the web UI
  - visible synced Facebook pages and Instagram IDs
  - connected FB + IG shown together, unconnected assets shown separately
  - per-row Google Drive folder mapping
  - per-row daily posting frequency
  - 9 AM Telegram report of previous-day posting health and counts
- Prefer implementation choices that can be hardened for production later.
