# Security notes for slack-channel-export

This file documents the threat model assumptions encoded in the app.

## OAuth state binding

The OAuth `state` parameter is a 24-byte URL-safe nonce. It is paired with a
signed cookie (`oauth_state`) carrying `{nonce, flow}`. On callback we verify:

1. The cookie signature is valid (itsdangerous, salt `oauth-state-v1`).
2. The cookie is no older than 5 minutes (TTL enforced via `max_age`).
3. The `flow` field matches the callback route (`"export"` or `"rejoin"`).
4. The `nonce` matches the `state` query parameter Slack sent back.

This binds each OAuth handshake to a single browser and a single flow. A
state issued for the export flow cannot be used to complete the rejoin flow
or vice versa. The cookie is HttpOnly, SameSite=Lax, and Secure (unless the
operator opts into plain HTTP for local dev via `APP_ALLOW_INSECURE_COOKIES=1`).

The cookie is deleted on every terminal path: state-mismatch reject, post-
validation API failure, and success.

## In-memory stores

`_pending_downloads` and `_rejoin_sessions` are `EphemeralStore` instances —
TTL + hard size cap + lazy sweep, lock-protected. They are process-local,
which is why the Dockerfile runs gunicorn with `--workers 1`. Adding workers
will break the download stash and rejoin sessions across worker boundaries.

If we ever need to scale beyond a single worker, both stores need to move
to a shared backend (Redis or a signed cookie carrying the rejoin token
directly). Don't bump `--workers` without that change.

## Rejoin session token

The Slack user token granted during the rejoin flow lives in memory in
`_rejoin_sessions` for at most 15 minutes. It is wiped (`discard`) as soon
as the upload completes, and `auth_revoke` is called best-effort. Revoke
failures are logged at WARNING so they're visible in incident response.

## CSV injection

CSV cells are passed through `_sanitize_csv_cell` at write time before being
written to the export file. A leading `=`, `+`, `-`, `@`, `\t`, or `\r` in
any string cell is prefixed with `'` so Excel, Sheets, and Numbers all treat
the cell as text rather than a formula. Only the `purpose` field is currently
free-form on Slack's side; the rest is constrained, but sanitizing the whole
row keeps the rule simple.

## Upload limits

- `MAX_CONTENT_LENGTH = 1 MB` — Flask rejects oversized uploads with 413
  before the route handler even reads the body.
- `_MAX_REJOIN_ROWS = 5000` — even within a small file, we stop parsing
  past 5000 rows so a degenerate CSV can't push thousands of `conversations.join`
  calls under the user's token.

## Headers and CSP

A strict CSP (`default-src 'none'; style-src 'unsafe-inline'; img-src 'self';
form-action 'self'; frame-ancestors 'none'; base-uri 'none'`) is applied via
`@app.after_request`. We do not use any inline scripts; the auto-redirect to
the download is a `<meta http-equiv="refresh">`. Other standard headers:
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
no-referrer`.

## Scope verification

After the OAuth code exchange, we verify the user actually granted every
scope we asked for (`USER_SCOPES` for export, `REJOIN_SCOPES` for rejoin).
A user who declines a subset is rejected with a 400 listing the missing
scopes, so we never proceed with a token that can't do the work.

## What this app deliberately does not do

- No persistent storage. All artifacts live for the request lifetime only.
- No DM/message read scopes — only channel-list metadata plus write access
  to the user's own DM channel (and `channels:write` on the rejoin side).
- No admin-level scopes.
- No telemetry. The only logs are the OAuth-completion line, error
  conditions, and the dev-mode startup banner.
