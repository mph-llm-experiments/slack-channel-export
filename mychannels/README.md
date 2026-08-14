# /mychannels — Slack channel list slash command

Slack Bolt app backing the `/mychannels` slash command:

- `/mychannels` — replies (ephemerally) with every public and private channel you're a member of, grouped by visibility, with member counts and channel purposes.
- `/mychannels @user` — the same list for someone else. Restricted to workspace admins.

Built as a companion to the export/rejoin tool at the repo root: run it *before* someone's account is deactivated, since Slack drops a deactivated user's channel-membership visibility.

## How it works

A single Flask-less Bolt app (`app.py`) with the channel fetching/formatting split into `channels.py`. Two tokens:

- **Bot token** (`xoxb-...`) handles the slash command, admin checks (`users.info`), and DM fallback (`chat.postMessage`).
- **User token** (`xoxp-...`) does the actual `users.conversations` lookup — this is what grants visibility into private channels, so it must come from a workspace admin's install.

Responses are ephemeral. If the formatted list would exceed Slack's 40k-character message limit, the reply is truncated and the full list is DM'd to the requester instead.

## Slack app config

Bot Token Scopes: `commands`, `users:read`, `chat:write`
User Token Scopes: `channels:read`, `groups:read`

The installing user **must be a workspace admin** for full private-channel visibility. See [docs/DEPLOY.md](./docs/DEPLOY.md) for the full walkthrough (Slack app creation → Cloud Run → wiring the slash command URL).

## Environment variables

- `SLACK_BOT_TOKEN` — bot token from the app's OAuth page
- `SLACK_USER_TOKEN` — user token from an admin install
- `SLACK_SIGNING_SECRET` — from Basic Information; verifies requests are from Slack
- `PORT` — listen port (defaults to 3000 locally; Cloud Run injects its own)

## Local dev

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
export SLACK_BOT_TOKEN=... SLACK_USER_TOKEN=... SLACK_SIGNING_SECRET=...
venv/bin/python app.py
```

Slash commands need a public URL — use something like `cloudflared tunnel` or deploy to Cloud Run and point the command there.

## Tests

```bash
venv/bin/pytest -q
```

Run from this directory. The suite is offline — Slack clients are mocked. (Running pytest from the repo root only collects the export app's tests; the root `pytest.ini` pins `testpaths = tests`.)

## Deploy

Own Cloud Run service, deployed from the repo root with `--source mychannels`. Full steps, including the Slack-side setup, in [docs/DEPLOY.md](./docs/DEPLOY.md).

## Project files

```
app.py              slash command handler (admin gate, mention parsing, DM fallback)
channels.py         channel fetching (paginated users.conversations) + formatting
Dockerfile          python:3.12-slim + gunicorn via the Bolt WSGI adapter
docs/DEPLOY.md      Slack app + Cloud Run deployment walkthrough
tests/              pytest suite (mocked Slack clients)
```
