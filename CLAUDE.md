# CLAUDE.md

Notes for future AI-assisted edits in this repo.

## What this is

Two Slack tools, two Cloud Run services, one repo:

- **Repo root**: single-file Flask app — `slack_channel_export_selfservice_1.py` — that runs both an export flow and a rejoin flow for Slack channel membership. Deployed to Google Cloud Run (service `slack-channel-export`, project `slack-channel-export`, us-west1), fronted by Cloud IAP for end-user auth.
- **`mychannels/`**: Slack Bolt app backing the `/mychannels` slash command. Own Dockerfile, tests, and deploy doc (`mychannels/docs/DEPLOY.md`). Deploy it with `--source mychannels`.

Everything below this line is about the export/rejoin app unless it says otherwise. Read [ARCHITECTURE.md](./ARCHITECTURE.md) for its design + threat model. README.md has deploy steps.

Root `pytest.ini` pins `testpaths = tests`, so running pytest at the root exercises only the export app. Run the mychannels suite from inside `mychannels/` with its own venv.

## Conventions worth keeping

- **No server-side persistence.** Everything lives in process memory: OAuth state store, rejoin session map. If a future change needs to persist anything, raise the concern explicitly — it changes the data-handling story in ARCHITECTURE.md and the threat model.
- **Single-instance Cloud Run on purpose.** `--min-instances=1 --max-instances=1`. The in-memory stores aren't safe across instances. Don't bump max-instances without also moving state to a shared backend.
- **Two flows, two scope sets, two redirect URLs.** Don't merge them. Export needs file/DM scopes; rejoin needs `channels:write`. Asking for the union expands what users grant.
- **Tokens get revoked.** Both flows call `auth_revoke()` (best-effort) on the user token as soon as the work is done. Keep doing this.
- **`_absolute_url()` builds redirect URIs.** It relies on `ProxyFix` so `request.url_root` reports `https://` behind Cloud Run/IAP. Don't bypass it or hardcode URLs.
- **Don't log secrets, tokens, channel content, or user PII.** The only stdout logs are the granted-scopes line (useful for diagnosing OAuth scope issues) and upload failures.

## Local dev

```bash
.venv/bin/python slack_channel_export_selfservice_1.py
```

Listens on `:5001` (not 5000 — macOS AirPlay claims that port and returns 403). For local OAuth, the Slack app's redirect URLs must include `http://localhost:5001/slack/callback` and `http://localhost:5001/slack/rejoin_callback`.

If you change file structure or add modules, update the Dockerfile's `COPY` step too — it currently copies only the single `.py` file.

## Gotchas we've already hit

- **AirPlay on port 5000.** macOS System Settings → General → AirDrop & Handoff → AirPlay Receiver. Either turn it off or stay on 5001.
- **Stale OAuth tab.** Slack's authorize URL embeds the scope set in `user_scope=…`. If you change `USER_SCOPES` and then click an old tab's authorize button, you'll grant the old scopes. Always start from `/` after a scope change.
- **`files:write` alone isn't enough to send a file to a conversation.** You also need a `*:write` scope for the conversation type. For the self-DM upload we need `im:write`.
- **`conversations.join` is idempotent.** Already-a-member returns `ok:true` with `warning: already_in_channel`, not a `SlackApiError`. Inspect the response's `response_metadata.warnings` to distinguish a real join from a no-op.
- **Multiple redirect URLs require explicit `redirect_uri`.** Once the Slack app has more than one URL configured, both `AuthorizeUrlGenerator.generate()` and `oauth.v2.access()` must be called with `redirect_uri=...`.
- **Cloud Run default timeout is 5 min.** For large rejoin runs (rate-limited at Slack's tier), use `--timeout 3600`.

## Deploy

```bash
gcloud run deploy slack-channel-export --source . --region us-west1
```

(Flags from the initial deploy are sticky.)

## Tests

A `pytest` suite lives under `tests/`. It uses Flask's in-process test client and a `mock_web_client` fixture in `tests/conftest.py` that monkeypatches `slack_sdk.WebClient` so tests run offline. Run:

```bash
.venv/bin/pytest -q
```

Install dev deps with `pip install -r requirements-dev.txt`. If you add a new code path that touches OAuth, an in-memory store, the CSV writer, or any external HTTP, add or extend a test for it.

## When in doubt

- Touching OAuth, scopes, or redirect handling? Reread ARCHITECTURE.md's "Slack OAuth flows" section first.
- Touching secrets or service accounts? They live in Secret Manager and the runtime SA has `secretAccessor` only — don't broaden.
- Touching IAP or who can reach the service? That's set by IAM bindings on the IAP resource — surface the change explicitly so it can go through the same review channel as the rest.
