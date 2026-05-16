#!/usr/bin/env python3
"""
Self-service Slack channel membership export.

A user heading out on leave or sabbatical authorizes via Slack OAuth and
gets a CSV of all their channel memberships (public + private channels)
delivered as a Slack DM to themselves, along with a Markdown checklist
for the private channels they'll need help rejoining. Nothing is
persisted server-side.

Requirements:
    pip install flask slack-sdk

Setup:
    1. Create a Slack app at https://api.slack.com/apps
    2. Under "OAuth & Permissions", add these *User Token Scopes*:
       - channels:read
       - groups:read
       - users:read
       - files:write
       - im:write
    3. Add a Redirect URL: http://localhost:5001/slack/callback
       (or your real hostname for production)
    4. Copy your Client ID and Client Secret from "Basic Information"
    5. Set environment variables:
       export SLACK_CLIENT_ID=your-client-id
       export SLACK_CLIENT_SECRET=your-client-secret

Usage:
    python slack_channel_export_selfservice.py
    # User visits http://localhost:5001
    # Clicks "Export My Channels"
    # Authorizes with Slack
    # CSV + Markdown checklist are DM'd to them; one-shot CSV download in the browser
"""

import csv
import io
import logging
import os
import secrets
import sys
import threading
import time
from collections import Counter
from datetime import datetime

from flask import Flask, make_response, redirect, request, send_file, render_template_string, url_for
from itsdangerous import BadData, URLSafeTimedSerializer
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.oauth import AuthorizeUrlGenerator
from werkzeug.middleware.proxy_fix import ProxyFix


logger = logging.getLogger("slack_channel_export")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class EphemeralStore:
    """Thread-safe in-memory store with per-entry TTL and a hard size cap.

    Holds short-lived per-request artifacts (CSV download payloads, rejoin
    session tokens) without unbounded memory growth. Lazy sweep on every op
    so a quiet period followed by a burst still self-cleans.
    """

    def __init__(self, ttl_seconds: float, max_size: int = 1000):
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._items: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value: object) -> None:
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            if (
                len(self._items) >= self._max_size
                and key not in self._items
            ):
                # O(n) scan to find the oldest entry; acceptable at max_size <= 1000.
                oldest_key = min(self._items, key=lambda k: self._items[k][0])
                del self._items[oldest_key]
            self._items[key] = (now + self._ttl, value)

    def pop(self, key: str) -> object | None:
        with self._lock:
            self._sweep_locked(time.monotonic())
            entry = self._items.pop(key, None)
        return entry[1] if entry is not None else None

    def peek(self, key: str) -> object | None:
        with self._lock:
            self._sweep_locked(time.monotonic())
            entry = self._items.get(key)
        return entry[1] if entry is not None else None

    def discard(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def _sweep_locked(self, now: float) -> None:
        """Drop every entry whose TTL has passed. After this returns, every
        remaining entry has `exp >= now`, so callers can read without further
        staleness checks."""
        expired = [k for k, (exp, _) in self._items.items() if exp < now]
        for k in expired:
            del self._items[k]


app = Flask(__name__)
# Cloud Run / IAP terminate TLS upstream; trust their X-Forwarded-* headers so
# url_for(..., _external=True) returns https:// URLs that match the Slack app's
# configured redirect URIs.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_urlsafe(32)
# Channel exports are small (a few KB) — 1 MB is generous and stops a casual
# attacker from OOM'ing the worker via the rejoin upload endpoint.
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024


@app.after_request
def _add_security_headers(resp):
    # Strict CSP: we serve no scripts, no third-party assets. style-src
    # 'unsafe-inline' allows the small inline <style> blocks in templates.
    # form-action 'self' limits where forms can submit. frame-ancestors 'none'
    # blocks clickjacking via iframe embedding.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "img-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


if not os.environ.get("APP_SECRET_KEY"):
    # Ephemeral key: fine for dev, but in-flight OAuth flows break on restart.
    # Production should set APP_SECRET_KEY explicitly.
    logger.warning("APP_SECRET_KEY not set; using an ephemeral key")

_OAUTH_STATE_TTL = 300
_OAUTH_STATE_COOKIE = "oauth_state"
_OAUTH_STATE_SALT = "oauth-state-v1"


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.secret_key, salt=_OAUTH_STATE_SALT)


def issue_oauth_state(flow: str) -> tuple[str, str]:
    """Mint a (nonce, signed_cookie_value) pair for an outgoing OAuth redirect.

    The nonce goes in the Slack ``state`` query parameter; the signed cookie
    binds the flow to the user's browser. Both must match on callback.
    """
    nonce = secrets.token_urlsafe(24)
    signed = _state_serializer().dumps({"nonce": nonce, "flow": flow})
    return nonce, signed


def consume_oauth_state(
    flow: str, query_state: str | None, signed_cookie: str | None
) -> bool:
    """Return True iff the cookie's signature is valid, fresh, bound to ``flow``,
    and its embedded nonce matches the query_state."""
    if not query_state or not signed_cookie:
        return False
    try:
        payload = _state_serializer().loads(
            signed_cookie, max_age=_OAUTH_STATE_TTL
        )
    except BadData:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("flow") == flow
        and payload.get("nonce") == query_state
    )


def _cookie_secure() -> bool:
    """Cookies should always be Secure in prod. Operator opts in to plain HTTP
    only for local dev via APP_ALLOW_INSECURE_COOKIES=1."""
    return os.environ.get("APP_ALLOW_INSECURE_COOKIES") != "1"


def _error_response_clearing_state(message: str, status: int):
    """Render ERROR_PAGE and clear the oauth_state cookie. Use on any failure
    path after consume_oauth_state has succeeded — the cookie has been logically
    consumed and shouldn't linger in the browser."""
    resp = make_response(render_template_string(ERROR_PAGE, error=message), status)
    resp.delete_cookie(_OAUTH_STATE_COOKIE)
    return resp


def _missing_scopes(oauth_resp: dict, required: list[str]) -> list[str]:
    """Return required scopes Slack did NOT grant. Empty list means OK."""
    granted = {
        s.strip()
        for s in oauth_resp.get("authed_user", {}).get("scope", "").split(",")
        if s.strip()
    }
    return sorted(set(required) - granted)


CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")

# One-shot in-memory stash for the browser download. CSV is also delivered via
# Slack DM; this just keeps the "Download Again" link working for a single fetch.
# Capped + TTL'd so an abandoned auto-redirect doesn't pin CSV bytes in memory
# indefinitely.
_pending_downloads: EphemeralStore = EphemeralStore(ttl_seconds=300, max_size=500)

# Short-lived session for the /rejoin flow, bridging OAuth callback → file
# upload. Capped + TTL'd so orphaned auths don't hold channels:write tokens
# in memory forever.
_REJOIN_SESSION_COOKIE = "rejoin_sid"
_REJOIN_SESSION_TTL = 900  # 15 min — long enough to grab the CSV from Slack
_rejoin_sessions: EphemeralStore = EphemeralStore(
    ttl_seconds=_REJOIN_SESSION_TTL, max_size=500
)
_MAX_REJOIN_ROWS = 5000  # generous: well above any realistic Slack workspace

_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(value):
    """Defuse CSV formula injection by prefixing risky cells with a single quote.

    Excel/Sheets/Numbers interpret a leading =, +, -, @ (and some control chars)
    as a formula. The leading quote forces text interpretation in all three.
    """
    if isinstance(value, str) and value and value[0] in _CSV_DANGEROUS_PREFIXES:
        return "'" + value
    return value


USER_SCOPES = [
    "channels:read",
    "groups:read",
    "users:read",
    "files:write",
    "im:write",
]

REJOIN_SCOPES = [
    "channels:write",
]

LANDING_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Slack Channel Export</title>
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #333; }
        h1 { font-size: 24px; }
        p { line-height: 1.6; color: #555; }
        .btn { display: inline-block; background: #4A154B; color: white; padding: 12px 24px;
               border-radius: 6px; text-decoration: none; font-size: 16px; margin-top: 16px; }
        .btn:hover { background: #3a1139; }
        .info { background: #f5f5f5; padding: 16px; border-radius: 8px; margin: 20px 0; }
    </style>
</head>
<body>
    <h1>Slack Channel Export</h1>
    <p>Heading out on leave or sabbatical? Export a list of every public and private
       Slack channel you belong to so you can rejoin them when you're back.</p>
    <div class="info">
        <strong>What this does:</strong>
        <ul>
            <li>Exports a CSV of every channel you're in (public + private)</li>
            <li>Includes a friendly Markdown checklist for the private channels you'll need help getting back into</li>
            <li>DMs both files to you in Slack and offers a one-time CSV download</li>
            <li>Does NOT export DMs, group DMs, or any message content</li>
        </ul>
        <strong>Heads up:</strong> nothing is stored server-side. This tool hands
        you the files and walks away — keep your own copy.
    </div>
    <a class="btn" href="/slack/auth">Export My Channels</a>
    <p style="margin-top: 32px; font-size: 14px; color: #888;">
        Coming back from leave? <a href="/rejoin">Rejoin your channels →</a>
    </p>
</body>
</html>
"""

DONE_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Export Complete</title>
    <meta http-equiv="refresh" content="0; url=/download/{{ token }}">
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #333; }
        h1 { font-size: 24px; }
        p { line-height: 1.6; color: #555; }
        .stats { background: #f0faf0; padding: 16px; border-radius: 8px; margin: 20px 0; }
        .btn { display: inline-block; background: #4A154B; color: white; padding: 12px 24px;
               border-radius: 6px; text-decoration: none; font-size: 16px; margin-top: 16px; }
    </style>
</head>
<body>
    <h1>Export Complete</h1>
    <div class="stats">
        <strong>{{ user_name }}</strong> ({{ user_id }})<br>
        {{ total }} conversations exported:<br>
        {% for type, count in counts.items() %}
            &nbsp;&nbsp;{{ type }}: {{ count }}<br>
        {% endfor %}
    </div>
    {% if dm_status == "sent" %}
      <p>Your CSV and welcome-back Markdown checklist have been DM'd to you in Slack.
         The browser download for the CSV should also start automatically — save it now,
         nothing is stored on this server.</p>
    {% else %}
      <p>Your CSV download should start automatically. Save it now — nothing is stored on this server.
         {% if dm_status %}<br><small>(Slack DM {{ dm_status }}.)</small>{% endif %}</p>
    {% endif %}
    <a class="btn" href="/download/{{ token }}">Download</a>
</body>
</html>
"""

ERROR_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Export Error</title>
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #333; }
        h1 { font-size: 24px; color: #c00; }
        .error { background: #fef0f0; padding: 16px; border-radius: 8px; }
        a { color: #4A154B; }
    </style>
</head>
<body>
    <h1>Something went wrong</h1>
    <div class="error">{{ error }}</div>
    <p><a href="/">Try again</a></p>
</body>
</html>
"""

REJOIN_LANDING_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Rejoin Slack Channels</title>
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #333; }
        h1 { font-size: 24px; }
        p { line-height: 1.6; color: #555; }
        .btn { display: inline-block; background: #4A154B; color: white; padding: 12px 24px;
               border-radius: 6px; text-decoration: none; font-size: 16px; margin-top: 16px; }
        .btn:hover { background: #3a1139; }
        .info { background: #f5f5f5; padding: 16px; border-radius: 8px; margin: 20px 0; }
    </style>
</head>
<body>
    <h1>Welcome back!</h1>
    <p>Upload the channel export CSV you got before you left. This tool will auto-rejoin
       every public channel from that list. For private channels you'll need a human to
       invite you back — the Markdown checklist that came with your export walks you through it.</p>
    <div class="info">
        <strong>What this does:</strong>
        <ul>
            <li>Authorizes with Slack to act on your behalf</li>
            <li>Auto-joins every public channel listed in your CSV</li>
            <li>Skips channels you're already in or that are archived</li>
            <li>Does NOT touch private channels, DMs, or group DMs</li>
        </ul>
    </div>
    <a class="btn" href="/rejoin/auth">Get started</a>
</body>
</html>
"""

REJOIN_UPLOAD_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Upload your channel CSV</title>
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #333; }
        h1 { font-size: 24px; }
        p { line-height: 1.6; color: #555; }
        .btn { display: inline-block; background: #4A154B; color: white; padding: 12px 24px;
               border-radius: 6px; text-decoration: none; border: 0; font-size: 16px; margin-top: 16px; cursor: pointer; }
        .btn:hover { background: #3a1139; }
        .err { background: #fef0f0; padding: 12px; border-radius: 6px; color: #c00; margin: 16px 0; }
        input[type=file] { font-size: 15px; }
    </style>
</head>
<body>
    <h1>Upload your channel CSV</h1>
    <p>Pick the <code>.csv</code> from the export you ran before you left. We'll join every
       public channel listed, skipping any you're already in.</p>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="post" enctype="multipart/form-data">
        <input type="file" name="csv" accept=".csv,text/csv" required>
        <br>
        <button class="btn" type="submit">Rejoin my public channels</button>
    </form>
</body>
</html>
"""

REJOIN_DONE_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Rejoin Complete</title>
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; max-width: 720px; margin: 80px auto; padding: 0 20px; color: #333; }
        h1 { font-size: 24px; }
        h2 { font-size: 18px; margin-top: 28px; }
        p { line-height: 1.6; color: #555; }
        .stats { background: #f0faf0; padding: 16px; border-radius: 8px; margin: 20px 0; }
        .warn { background: #fff8e6; padding: 12px; border-radius: 6px; margin: 16px 0; }
        ul.compact { columns: 2; column-gap: 24px; font-size: 14px; }
        ul.compact li { break-inside: avoid; }
        .btn { display: inline-block; background: #4A154B; color: white; padding: 12px 24px;
               border-radius: 6px; text-decoration: none; font-size: 16px; margin-top: 16px; }
    </style>
</head>
<body>
    <h1>Rejoin complete</h1>
    <div class="stats">
        <strong>{{ total }}</strong> public channels in your CSV<br>
        &nbsp;&nbsp;Joined: {{ joined|length }}<br>
        &nbsp;&nbsp;Already in: {{ skipped|length }}<br>
        &nbsp;&nbsp;Failed: {{ failed|length }}
    </div>
    {% if rate_limited %}
    <div class="warn">
        Slack rate-limited us partway through. Wait a few minutes and run this again
        with the same CSV — channels you've already rejoined will be skipped, and we'll
        pick up where we left off.
    </div>
    {% endif %}
    <p>For private channels, use the Markdown checklist that came with your export — those
       need a human in the channel to invite you back.</p>

    {% if joined %}
    <h2>Joined ({{ joined|length }})</h2>
    <ul class="compact">
    {% for name, cid in joined %}<li>#{{ name }}</li>{% endfor %}
    </ul>
    {% endif %}

    {% if failed %}
    <h2>Failed ({{ failed|length }})</h2>
    <ul>
    {% for name, cid, err in failed %}<li>#{{ name }} — <code>{{ err }}</code></li>{% endfor %}
    </ul>
    {% endif %}

    <a class="btn" href="/">Back to start</a>
</body>
</html>
"""


def build_markdown_checklist(rows: list[dict]) -> str:
    """Friendly welcome-back checklist. Public channels get auto-rejoined
    by the companion tool; private channels need a human to invite the
    returning user back in."""
    public = [r for r in rows if r["type"] == "Public Channel" and not r["is_archived"]]
    private = [r for r in rows if r["type"] == "Private Channel" and not r["is_archived"]]

    lines = [
        "# Welcome back!",
        "",
        "Here's the lay of the land for getting your Slack channels back. There are two parts:",
        "",
        f"- **{len(public)} public channel{'s' if len(public) != 1 else ''}** — these will be auto-rejoined for you when you run the rejoin tool. No action needed below.",
        f"- **{len(private)} private channel{'s' if len(private) != 1 else ''}** — these need a human to invite you back. The checklist below is your guide.",
        "",
        "## Private channels — your re-invite checklist",
        "",
        "For each of these, find someone you know who's still in the channel and ask them to add you back. "
        "If you're not sure who to ask, the channel's purpose (where shown) might jog your memory.",
        "",
    ]

    if private:
        for r in private:
            line = f"- [ ] **#{r['name']}** (`{r['channel_id']}`)"
            if r["purpose"]:
                line += f" — _{r['purpose']}_"
            lines.append(line)
    else:
        lines.append("_None — you weren't in any private channels._")

    lines += [
        "",
        "## Public channels — informational",
        "",
        "These will be auto-rejoined by the rejoin tool. Listed here so you can spot anything you no longer want to be in:",
        "",
    ]
    if public:
        for r in public:
            lines.append(f"- #{r['name']}")
    else:
        lines.append("_None._")

    lines += [
        "",
        "---",
        "",
        "_Generated by the Slack Channel Export tool. Nothing was stored server-side — this file is your only copy._",
        "",
    ]
    return "\n".join(lines)


@app.route("/")
def index():
    return render_template_string(LANDING_PAGE)


def _absolute_url(endpoint: str) -> str:
    """Build the public URL for a Flask endpoint, honoring X-Forwarded headers
    so this works behind Cloud Run / IAP."""
    return url_for(endpoint, _external=True)


@app.route("/slack/auth")
def slack_auth():
    nonce, signed = issue_oauth_state("export")
    generator = AuthorizeUrlGenerator(
        client_id=CLIENT_ID,
        user_scopes=USER_SCOPES,
        redirect_uri=_absolute_url("slack_callback"),
    )
    resp = redirect(generator.generate(state=nonce))
    resp.set_cookie(
        _OAUTH_STATE_COOKIE,
        signed,
        max_age=_OAUTH_STATE_TTL,
        httponly=True,
        secure=_cookie_secure(),
        samesite="Lax",
    )
    return resp


@app.route("/slack/callback")
def slack_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return render_template_string(ERROR_PAGE, error=f"Slack authorization failed: {error}"), 400

    signed_state = request.cookies.get(_OAUTH_STATE_COOKIE)
    if not consume_oauth_state("export", state, signed_state):
        return _error_response_clearing_state(
            "Invalid or expired state. Please try again.", 400
        )

    # Exchange code for user token
    client = WebClient()
    try:
        oauth_resp = client.oauth_v2_access(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code=code,
            redirect_uri=_absolute_url("slack_callback"),
        )
    except SlackApiError as e:
        return _error_response_clearing_state(
            f"OAuth error: {e.response['error']}", 500
        )

    missing = _missing_scopes(oauth_resp, USER_SCOPES)
    if missing:
        return _error_response_clearing_state(
            f"Missing required scopes: {', '.join(missing)}.", 400
        )

    user_token = oauth_resp.get("authed_user", {}).get("access_token")
    user_id = oauth_resp.get("authed_user", {}).get("id")
    granted_scopes = oauth_resp.get("authed_user", {}).get("scope", "")
    logger.info(
        "oauth completed user_id=%s granted_scopes=%s", user_id, granted_scopes
    )

    if not user_token:
        return _error_response_clearing_state("No user token received.", 500)

    user_client = WebClient(token=user_token)

    # Get user profile for filename/display
    try:
        user_info = user_client.users_info(user=user_id)
        user_name = user_info["user"]["profile"].get("real_name", user_id)
    except SlackApiError as e:
        logger.info(
            "users_info failed, falling back to user_id: %s",
            e.response.get("error", "unknown") if hasattr(e, "response") else "unknown",
        )
        user_name = user_id

    # Fetch all channels (public + private only — DMs/group DMs are out of scope)
    channels = []
    cursor = None
    while True:
        try:
            resp = user_client.users_conversations(
                types="public_channel,private_channel",
                limit=200,
                cursor=cursor,
            )
        except SlackApiError as e:
            return _error_response_clearing_state(
                f"API error: {e.response['error']}", 500
            )

        channels.extend(resp["channels"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    rows = []
    for ch in channels:
        ch_type = "Private Channel" if ch.get("is_private") else "Public Channel"
        name = ch.get("name_normalized") or ch.get("name", ch["id"])
        rows.append({
            "type": ch_type,
            "name": name,
            "channel_id": ch["id"],
            "is_archived": ch.get("is_archived", False),
            "num_members": ch.get("num_members", ""),
            "purpose": ch.get("purpose", {}).get("value", ""),
        })

    type_order = {"Public Channel": 0, "Private Channel": 1}
    rows.sort(key=lambda r: (type_order.get(r["type"], 99), r["name"].lower()))

    # Build CSV + Markdown checklist in memory — nothing is persisted server-side.
    datestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in user_name)
    base = f"{safe_name}_{user_id}_{datestamp}"
    csv_filename = f"{base}.csv"
    md_filename = f"{base}.md"

    fieldnames = ["type", "name", "channel_id", "is_archived", "num_members", "purpose"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    # Sanitize at write time only — leaves `rows` untouched so the markdown
    # checklist below still renders channel names without a leading quote.
    sanitized_rows = [
        {k: _sanitize_csv_cell(v) for k, v in row.items()}
        for row in rows
    ]
    writer.writerows(sanitized_rows)
    csv_bytes = buf.getvalue().encode("utf-8")

    md_bytes = build_markdown_checklist(rows).encode("utf-8")

    counts = dict(Counter(r["type"] for r in rows))

    # DM both files to the user. Two uploads = two file attachments in the
    # same self-DM; the first carries the friendly intro comment.
    dm_status = None
    try:
        im = user_client.conversations_open(users=user_id)
        dm_channel = im["channel"]["id"]
        user_client.files_upload_v2(
            channel=dm_channel,
            content=md_bytes,
            filename=md_filename,
            title="Welcome-back checklist",
            initial_comment=(
                f"Here's your Slack channel export ({len(rows)} channels). "
                "Read the Markdown checklist first — it walks you through what to do when you're back. "
                "The CSV is the full machine-readable list."
            ),
        )
        user_client.files_upload_v2(
            channel=dm_channel,
            content=csv_bytes,
            filename=csv_filename,
            title="Channel membership (CSV)",
        )
        dm_status = "sent"
    except SlackApiError as e:
        dm_status = f"failed: {e.response['error']}"
        logger.warning("slack file upload failed: %s", e.response.get("error", "unknown"))

    # Revoke the user token immediately — we don't need it anymore
    try:
        user_client.auth_revoke()
    except SlackApiError as e:
        logger.warning(
            "auth_revoke failed (export flow): %s",
            e.response.get("error", "unknown") if hasattr(e, "response") else "unknown",
        )

    # Stash CSV in memory for a single browser download. Token is opaque; the
    # entry is popped on first GET, and the store TTL-evicts orphaned entries.
    token = secrets.token_urlsafe(16)
    _pending_downloads.put(token, (csv_bytes, csv_filename))

    resp = make_response(
        render_template_string(
            DONE_PAGE,
            user_name=user_name,
            user_id=user_id,
            total=len(rows),
            counts=counts,
            token=token,
            dm_status=dm_status,
        )
    )
    resp.delete_cookie(_OAUTH_STATE_COOKIE)
    return resp


@app.route("/download/<token>")
def download(token):
    entry = _pending_downloads.pop(token)
    if entry is None:
        return render_template_string(
            ERROR_PAGE,
            error="Download link expired or already used. The CSV is in your Slack DMs.",
        ), 404
    csv_bytes, filename = entry
    return send_file(
        io.BytesIO(csv_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype="text/csv",
    )


@app.route("/rejoin")
def rejoin_index():
    return render_template_string(REJOIN_LANDING_PAGE)


@app.route("/rejoin/auth")
def rejoin_auth():
    nonce, signed = issue_oauth_state("rejoin")
    generator = AuthorizeUrlGenerator(
        client_id=CLIENT_ID,
        user_scopes=REJOIN_SCOPES,
        redirect_uri=_absolute_url("rejoin_callback"),
    )
    resp = redirect(generator.generate(state=nonce))
    resp.set_cookie(
        _OAUTH_STATE_COOKIE,
        signed,
        max_age=_OAUTH_STATE_TTL,
        httponly=True,
        secure=_cookie_secure(),
        samesite="Lax",
    )
    return resp


@app.route("/slack/rejoin_callback")
def rejoin_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return render_template_string(ERROR_PAGE, error=f"Slack authorization failed: {error}"), 400
    signed_state = request.cookies.get(_OAUTH_STATE_COOKIE)
    if not consume_oauth_state("rejoin", state, signed_state):
        return _error_response_clearing_state(
            "Invalid or expired state. Please try again.", 400
        )

    client = WebClient()
    try:
        oauth_resp = client.oauth_v2_access(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code=code,
            redirect_uri=_absolute_url("rejoin_callback"),
        )
    except SlackApiError as e:
        return _error_response_clearing_state(
            f"OAuth error: {e.response['error']}", 500
        )

    missing = _missing_scopes(oauth_resp, REJOIN_SCOPES)
    if missing:
        return _error_response_clearing_state(
            f"Missing required scopes: {', '.join(missing)}.", 400
        )

    user_token = oauth_resp.get("authed_user", {}).get("access_token")
    user_id = oauth_resp.get("authed_user", {}).get("id")
    if not user_token:
        return _error_response_clearing_state("No user token received.", 500)

    sid = secrets.token_urlsafe(24)
    _rejoin_sessions.put(sid, {"token": user_token, "user_id": user_id})
    resp = redirect(url_for("rejoin_upload"))
    resp.set_cookie(
        _REJOIN_SESSION_COOKIE,
        sid,
        max_age=_REJOIN_SESSION_TTL,
        httponly=True,
        secure=_cookie_secure(),
        samesite="Lax",
    )
    resp.delete_cookie(_OAUTH_STATE_COOKIE)
    return resp


def _get_rejoin_session() -> tuple[str | None, dict | None]:
    sid = request.cookies.get(_REJOIN_SESSION_COOKIE)
    if not sid:
        return None, None
    session = _rejoin_sessions.peek(sid)
    if session is None:
        return None, None
    return sid, session


@app.route("/rejoin/upload", methods=["GET", "POST"])
def rejoin_upload():
    sid, session = _get_rejoin_session()
    if session is None:
        return render_template_string(
            ERROR_PAGE,
            error='Session expired. Start over at <a href="/rejoin">/rejoin</a>.',
        ), 401

    if request.method == "GET":
        return render_template_string(REJOIN_UPLOAD_PAGE)

    file = request.files.get("csv")
    if not file or not file.filename:
        return render_template_string(REJOIN_UPLOAD_PAGE, error="Please pick a CSV file."), 400

    try:
        text = file.read().decode("utf-8")
    except UnicodeDecodeError:
        return render_template_string(REJOIN_UPLOAD_PAGE, error="That file isn't UTF-8 CSV."), 400

    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for row in reader:
        if len(rows) >= _MAX_REJOIN_ROWS:
            return render_template_string(
                REJOIN_UPLOAD_PAGE,
                error=f"CSV exceeds {_MAX_REJOIN_ROWS} rows. Trim and try again.",
            ), 400
        rows.append(row)

    public_rows = [
        r for r in rows
        if r.get("type") == "Public Channel"
        and r.get("is_archived", "False") != "True"
    ]

    user_client = WebClient(token=session["token"])
    joined: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    rate_limited = False

    for row in public_rows:
        cid = row.get("channel_id", "")
        name = row.get("name", cid)
        if not cid:
            continue
        try:
            resp = user_client.conversations_join(channel=cid)
            # Slack returns ok=true with a warning (not an error) when you're
            # already a member, so success != fresh join.
            warnings = resp.get("response_metadata", {}).get("warnings") or []
            if "already_in_channel" in warnings or resp.get("warning") == "already_in_channel":
                skipped.append((name, cid))
            else:
                joined.append((name, cid))
        except SlackApiError as e:
            err = e.response.get("error", "unknown") if hasattr(e, "response") else "unknown"
            if err == "ratelimited":
                rate_limited = True
                break
            else:
                failed.append((name, cid, err))

    # Best-effort token revoke + session cleanup
    try:
        user_client.auth_revoke()
    except SlackApiError as e:
        logger.warning(
            "auth_revoke failed (rejoin flow): %s",
            e.response.get("error", "unknown") if hasattr(e, "response") else "unknown",
        )
    _rejoin_sessions.discard(sid)

    html = render_template_string(
        REJOIN_DONE_PAGE,
        total=len(public_rows),
        joined=joined,
        skipped=skipped,
        failed=failed,
        rate_limited=rate_limited,
    )
    # Clear the session cookie now that we're done
    resp = make_response(html)
    resp.delete_cookie(_REJOIN_SESSION_COOKIE)
    return resp


if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET", file=sys.stderr)
        sys.exit(1)
    debug = os.environ.get("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")
    port = int(os.environ.get("PORT", "5001"))
    logger.info("Running at http://localhost:%d (debug=%s)", port, debug)
    app.run(host="127.0.0.1", port=port, debug=debug)
