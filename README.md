# parfemy-outreach

Automatic sample requests to fragrance houses, running in GitHub Actions.

- `outreach_cloud.py` - sends at most 400 e-mails per rolling 24 h (Gmail SMTP), reads replies (IMAP), classifies them and posts a Discord notification **only for YES replies**.
- `statebox.py` - the state (contacts, sent log, replies) is stored **encrypted** (`state.enc` on branch `state`). Nothing readable is committed.
- `.github/workflows/outreach.yml` - ~5.5 h runs chained one after another, cron as a fallback.

Secrets: `STATE_KEY`, `GMAIL_APP_PASSWORD`, `DISCORD_WEBHOOK`, `SELF_TOKEN`.
