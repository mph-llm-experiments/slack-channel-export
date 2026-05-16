# Architecture & Threat Model

For security review. The goal of this doc is to make every data path, every secret, and every defended-vs-undefended threat explicit.

The 2026-05-15 security-hardening change set introduced cookie-bound OAuth state, `EphemeralStore` for in-memory stashes (TTL + size cap), CSV formula-injection sanitization, OAuth scope verification, strict CSP and other security headers, and exact dependency pinning. See `docs/SECURITY-NOTES.md` for the current threat model.

## What this service is

A small self-service tool that lets a Puddingtime employee:

- **Export** their Slack channel membership (public + private channels only) as a CSV plus a Markdown welcome-back checklist, delivered as a Slack DM to themselves.
- **Rejoin** every public channel from that CSV in one click upon returning from leave.

Used at most a few times per person per year. Volume is low; correctness and minimal data retention matter more than scale.

## Components

```
   ┌───────────────┐    Google login          ┌──────────────────┐
   │ End user      │ ───────────────────────► │ Cloud IAP         │
   │ (browser)     │ ◄─────── 401 if not ────│ (puddingtime.net    │
   └───────────────┘          @puddingtime.net   │  domain only)    │
          │                                   └──────────────────┘
          │ once IAP-authenticated                   │
          ▼                                          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ Cloud Run service (us-west1, --min/max-instances=1)         │
   │ ┌────────────────────────────────────────────────────────┐ │
   │ │ Flask app (gunicorn, 1 worker, 8 threads)              │ │
   │ │  - in-memory OAuth state store (5 min TTL)             │ │
   │ │  - in-memory CSV download stash (one-shot, popped)     │ │
   │ │  - in-memory rejoin session store (15 min TTL)         │ │
   │ └────────────────────────────────────────────────────────┘ │
   └─────────────────────────────────────────────────────────────┘
          │             │                              │
          ▼             ▼                              ▼
   ┌──────────┐   ┌──────────────┐         ┌──────────────────────┐
   │ Secret   │   │ Slack OAuth  │         │ Slack Web API        │
   │ Manager  │   │ (Puddingtime    │         │ (user token, lasts   │
   │ (CIDs)   │   │ workspace)   │         │ ~seconds, revoked)   │
   └──────────┘   └──────────────┘         └──────────────────────┘
```

No database. No file storage. No cache. No queue.

## End-user access control: Cloud IAP

The Cloud Run service is fronted by Cloud IAP. IAP enforces:

- Authentication via Google OAuth against the `puddingtime.net` Google Workspace.
- Authorization via an IAM binding granting `roles/iap.httpsResourceAccessor` to `domain:puddingtime.net`.

A user who is not signed in, or is signed in but not part of `puddingtime.net`, gets a Google login page (then a 403) and never reaches the Flask app. Slack OAuth would be a second gate, but IAP is the primary one.

**Trade-off accepted:** an employee whose Google account has already been disabled cannot use this tool. This matches the intended use case (going on leave / sabbatical, not full offboarding).

## Slack OAuth flows

Two flows, two redirect URLs, two scope sets. They share the in-memory state store but otherwise don't interact.

### Export (`/slack/auth` → `/slack/callback`)

User scopes requested:

| Scope | Why |
|---|---|
| `channels:read` | enumerate public channels the user is in |
| `groups:read` | enumerate private channels the user is in |
| `users:read` | resolve the user's own display name for the filename |
| `files:write` | upload the CSV + Markdown as Slack files |
| `im:write` | open and post to the user's self-DM |

Server actions in the callback, in order:

1. Validate `state` (CSRF protection — a 24-byte URL-safe nonce in the Slack `state` query parameter, paired with a signed `oauth_state` cookie carrying `{nonce, flow}` — itsdangerous, salt `oauth-state-v1`, 5-min TTL). On callback: signature, age, flow match, and nonce match are all verified; the cookie is deleted on every terminal path.
2. Exchange code for a user access token via `oauth.v2.access`.
3. Call `users.conversations` (paginated, `types=public_channel,private_channel`) to enumerate channels.
4. Build CSV + Markdown checklist *in memory only*.
5. Open self-DM via `conversations.open(users=<self>)` and `files.upload_v2` twice (MD then CSV).
6. `auth.revoke()` the user token (best-effort).
7. Stash CSV bytes in a process-local `EphemeralStore` keyed by a `secrets.token_urlsafe(16)` token; render success page that auto-triggers a one-shot download.
8. The `/download/<token>` route pops the entry on first GET, after which it returns 404.

### Rejoin (`/rejoin/auth` → `/slack/rejoin_callback` → `/rejoin/upload`)

User scopes requested:

| Scope | Why |
|---|---|
| `channels:write` | `conversations.join` on public channels for the authorized user |

Server actions:

1. Same OAuth state CSRF protection as above.
2. Token exchange.
3. Stash `{"token": ..., "user_id": ...}` in `_rejoin_sessions` keyed by `secrets.token_urlsafe(24)`; TTL is owned by `EphemeralStore`, not stored as a field. Set as an `httpOnly`, `Secure` (gated on `_cookie_secure()` — env-var `APP_ALLOW_INSECURE_COOKIES`), `SameSite=Lax` cookie with a 15-min max-age.
4. Redirect to `/rejoin/upload`, which renders an HTTP file-upload form.
5. POST handler reads the uploaded CSV in memory, filters to `type == Public Channel` and `is_archived != True`, calls `conversations.join` per row. Idempotent: Slack returns `ok:true, warning:already_in_channel` for channels you're already in, which is counted as "Already in" rather than a fresh join.
6. Rate-limit (`error: ratelimited`) aborts the loop and the result page surfaces a "run again later" message — the operation is naturally resumable because already-joined channels are reported as such.
7. `auth.revoke()` and session pop after processing.

## Secrets

| Secret | Where it lives | How the service reads it |
|---|---|---|
| `SLACK_CLIENT_ID` | Secret Manager, `slack-client-id` | Mounted as env var via `--set-secrets` on Cloud Run |
| `SLACK_CLIENT_SECRET` | Secret Manager, `slack-client-secret` | Same |
| `APP_SECRET_KEY` | Secret Manager, `app-secret-key` | Same — signs the `oauth_state` cookie; generate with `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |

No service-account JSON keys are used. The Cloud Run runtime service account (`<project-number>-compute@developer.gserviceaccount.com`) has `roles/secretmanager.secretAccessor` on each secret only.

The Slack signing secret is not used — this app has no incoming Slack webhooks or slash commands, so request verification is N/A.

## Data handling

- **Slack user tokens** live only in process memory, for the duration of one HTTP request (export) or up to 15 minutes (rejoin, between OAuth and upload), and are revoked via `auth.revoke()` immediately after use.
- **CSV / Markdown files** are built in memory, sent to Slack as the user's own files, and stashed in memory only for the one-shot browser download. Nothing is written to disk inside the container.
- **No logs of channel content.** The app uses the `logging` module (format: `%(asctime)s %(levelname)s %(name)s %(message)s`) and emits lines such as `oauth completed user_id=… granted_scopes=…` and upload failure details to stdout (Cloud Logging captures stdout). No channel names, IDs, message content, or CSV bodies are logged.
- **Channel IDs in the CSV** are not secret per se — they're visible to any member of the workspace.

## Threats and mitigations

### In scope

| Threat | Mitigation |
|---|---|
| Unauthenticated outsider hits any route | Cloud IAP gates every request before Cloud Run sees it |
| Authenticated outsider (non-puddingtime.net Google account) | IAM `domain:puddingtime.net` binding on IAP |
| Employee A triggers an export for employee B | Slack OAuth — only the authorizing user's own tokens are issued; we only ever read the authorizing user's own conversations |
| OAuth code interception | `state` parameter is a 24-byte URL-safe nonce bound to a signed `oauth_state` cookie (`{nonce, flow}`, itsdangerous, salt `oauth-state-v1`, 5-min TTL); signature, age, flow match, and nonce match all verified before code exchange; cookie deleted on every terminal path |
| Cross-flow code replay (export code used at rejoin callback or vice versa) | `redirect_uri` is passed explicitly on both `AuthorizeUrlGenerator.generate()` and `oauth.v2.access()`; Slack enforces that the redirect URI must match what was used at authorize time |
| Token theft via logging or persistence | Tokens never written to disk or logs; revoked immediately after use |
| Download link sharing / replay | One-shot token, popped on first GET, then 404 |
| Session cookie theft | `httpOnly` + `Secure` (in prod) + `SameSite=Lax` + 15-min max-age; session is also explicitly invalidated server-side after the upload completes |
| Path traversal on download | Route uses opaque tokens as the key into an `EphemeralStore`, not a filesystem path |
| Stack overflow / large CSV upload | `MAX_CONTENT_LENGTH` is set to 1 MB; the row parser additionally caps at 5 000 rows |
| Slack API rate limits | Caught and surfaced; user can re-run with the same CSV and pick up where they left off |
| Pre-image of state values | All tokens (state nonce, download token, rejoin sid) use `secrets.token_urlsafe(N)` — CSPRNG, no UUID fallback |

### Out of scope / accepted

- **Multi-instance consistency.** In-memory state and download stash assume one process. We enforce this with `--min-instances=1 --max-instances=1`. If we ever lift that limit, we'd need to move state to a shared store. The README and deploy command both bake this in.
- **Long downtime during deploy.** Single instance means a brief gap on `gcloud run deploy`. Acceptable for a low-volume internal tool.
- **Compromised Slack OAuth app.** If the `SLACK_CLIENT_SECRET` leaks, an attacker could prompt users to authorize with the existing client ID; mitigated by access being IAP-gated, but rotation procedure (regenerate in Slack admin, update Secret Manager, redeploy) should be exercised.
- **Compromised Google Workspace account.** Out of scope; covered by Workspace IAM/IAP.
- **Departed users.** Intentional — see "End-user access control."

## Open issues to flag in review

1. **Consider granular scope `channels:write.invites` vs `channels:write`** for the rejoin flow. We use `channels:write` for `conversations.join`; the more granular split has moved over time in Slack's docs.
2. **Process-local session and download stash** survive only until the next instance restart. This is by design (lower retention is better) but means a redeploy mid-flow makes the user start over. Acceptable for the volume.
3. **No CSRF protection on the `/rejoin/upload` POST.** A future attacker page rendered in the user's browser cannot read the IAP-gated upload page (cross-origin), so the form submission can't be forged from off-site. If we ever exposed this without IAP, we'd need to add a CSRF token on the form.
4. **The IAP audience cookie used in OAuth callbacks** travels back with Slack's redirect. We rely on `SameSite=Lax` here. The IAP cookie itself is set by Google and not under our control.

## Dependencies

| Package | Purpose |
|---|---|
| `flask` | HTTP routing, templating |
| `slack-sdk` | Slack Web API client + OAuth helpers |
| `gunicorn` | Production WSGI server (Cloud Run) |
| `werkzeug` (transitive, via Flask) | `ProxyFix` for trusting Cloud Run's `X-Forwarded-*` headers |
