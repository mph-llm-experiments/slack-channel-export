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
import os
import secrets
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime

from flask import Flask, make_response, redirect, request, send_file, render_template_string, url_for
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.oauth import AuthorizeUrlGenerator
from werkzeug.middleware.proxy_fix import ProxyFix


class InMemoryOAuthStateStore:
    """Process-local OAuth state store with TTL. Replaces FileOAuthStateStore
    so the app has no filesystem dependency — fine for single-instance Cloud Run.
    """

    def __init__(self, expiration_seconds: int = 300):
        self.expiration_seconds = expiration_seconds
        self._states: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        state = str(uuid.uuid4())
        now = time.time()
        with self._lock:
            self._states[state] = now + self.expiration_seconds
            # Opportunistic cleanup of expired entries.
            for k in [k for k, exp in self._states.items() if exp < now]:
                self._states.pop(k, None)
        return state

    def consume(self, state: str) -> bool:
        with self._lock:
            exp = self._states.pop(state, None)
        return exp is not None and exp >= time.time()

app = Flask(__name__)
# Cloud Run / IAP terminate TLS upstream; trust their X-Forwarded-* headers so
# url_for(..., _external=True) returns https:// URLs that match the Slack app's
# configured redirect URIs.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")

# One-shot in-memory stash for the browser download: { token: (csv_bytes, filename) }.
# The CSV is also delivered via Slack DM; this dict just keeps the "Download Again"
# link working for a single fetch in the same session.
_pending_downloads: dict[str, tuple[bytes, str]] = {}

# Short-lived session for the /rejoin flow, bridging OAuth callback → file
# upload. Keyed by an opaque cookie; value holds the Slack user token.
_rejoin_sessions: dict[str, dict] = {}
_REJOIN_SESSION_COOKIE = "rejoin_sid"
_REJOIN_SESSION_TTL = 900  # 15 min — long enough to grab the CSV from Slack

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

state_store = InMemoryOAuthStateStore(expiration_seconds=300)

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
    state = state_store.issue()
    generator = AuthorizeUrlGenerator(
        client_id=CLIENT_ID,
        user_scopes=USER_SCOPES,
        redirect_uri=_absolute_url("slack_callback"),
    )
    url = generator.generate(state=state)
    return redirect(url)


@app.route("/slack/callback")
def slack_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return render_template_string(ERROR_PAGE, error=f"Slack authorization failed: {error}"), 400

    if not state_store.consume(state):
        return render_template_string(ERROR_PAGE, error="Invalid or expired state. Please try again."), 400

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
        return render_template_string(ERROR_PAGE, error=f"OAuth error: {e.response['error']}"), 500

    user_token = oauth_resp.get("authed_user", {}).get("access_token")
    user_id = oauth_resp.get("authed_user", {}).get("id")
    granted_scopes = oauth_resp.get("authed_user", {}).get("scope", "")
    print(f"[oauth] user={user_id} granted_scopes={granted_scopes}", flush=True)

    if not user_token:
        return render_template_string(ERROR_PAGE, error="No user token received."), 500

    user_client = WebClient(token=user_token)

    # Get user profile for filename/display
    try:
        user_info = user_client.users_info(user=user_id)
        user_name = user_info["user"]["profile"].get("real_name", user_id)
    except SlackApiError:
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
            return render_template_string(ERROR_PAGE, error=f"API error: {e.response['error']}"), 500

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
    writer.writerows(rows)
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
        print(f"[upload] failed: {e.response.data}", flush=True)

    # Revoke the user token immediately — we don't need it anymore
    try:
        user_client.auth_revoke()
    except SlackApiError:
        pass  # best effort

    # Stash CSV in memory for a single browser download. The token is opaque
    # and the entry is popped on first GET.
    token = secrets.token_urlsafe(16)
    _pending_downloads[token] = (csv_bytes, csv_filename)

    return render_template_string(
        DONE_PAGE
        + '<script>window.location.href="/download/{{ token }}";</script>',
        user_name=user_name,
        user_id=user_id,
        total=len(rows),
        counts=counts,
        token=token,
        dm_status=dm_status,
    )


@app.route("/download/<token>")
def download(token):
    entry = _pending_downloads.pop(token, None)
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
    state = state_store.issue()
    generator = AuthorizeUrlGenerator(
        client_id=CLIENT_ID,
        user_scopes=REJOIN_SCOPES,
        redirect_uri=_absolute_url("rejoin_callback"),
    )
    return redirect(generator.generate(state=state))


@app.route("/slack/rejoin_callback")
def rejoin_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return render_template_string(ERROR_PAGE, error=f"Slack authorization failed: {error}"), 400
    if not state_store.consume(state):
        return render_template_string(ERROR_PAGE, error="Invalid or expired state. Please try again."), 400

    client = WebClient()
    try:
        oauth_resp = client.oauth_v2_access(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code=code,
            redirect_uri=_absolute_url("rejoin_callback"),
        )
    except SlackApiError as e:
        return render_template_string(ERROR_PAGE, error=f"OAuth error: {e.response['error']}"), 500

    user_token = oauth_resp.get("authed_user", {}).get("access_token")
    user_id = oauth_resp.get("authed_user", {}).get("id")
    if not user_token:
        return render_template_string(ERROR_PAGE, error="No user token received."), 500

    sid = secrets.token_urlsafe(24)
    _rejoin_sessions[sid] = {
        "token": user_token,
        "user_id": user_id,
        "expires_at": time.time() + _REJOIN_SESSION_TTL,
    }
    resp = redirect(url_for("rejoin_upload"))
    resp.set_cookie(
        _REJOIN_SESSION_COOKIE,
        sid,
        max_age=_REJOIN_SESSION_TTL,
        httponly=True,
        secure=request.is_secure,
        samesite="Lax",
    )
    return resp


def _get_rejoin_session() -> tuple[str | None, dict | None]:
    sid = request.cookies.get(_REJOIN_SESSION_COOKIE)
    if not sid:
        return None, None
    session = _rejoin_sessions.get(sid)
    if not session or session["expires_at"] < time.time():
        _rejoin_sessions.pop(sid, None)
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
    rows = list(reader)
    public_rows = [
        r for r in rows
        if r.get("type") == "Public Channel" and r.get("is_archived", "False") != "True"
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
    except SlackApiError:
        pass
    _rejoin_sessions.pop(sid, None)

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
    print(f"Running at http://localhost:{port} (debug={debug})")
    app.run(host="127.0.0.1", port=port, debug=debug)
