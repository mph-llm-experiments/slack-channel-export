"""Integration-level tests using the Flask test client."""


def test_app_module_does_not_enable_debug_at_import_time(app):
    # Guards the production path: gunicorn imports this module without running
    # __main__, so any module-level `app.debug = True` would expose the Werkzeug
    # debugger to anyone hitting an exception. This test locks that in.
    assert app.debug is False


def test_pending_downloads_is_capped(app_mod):
    # The download stash must use EphemeralStore so memory can't grow unbounded.
    from slack_channel_export_selfservice_1 import EphemeralStore
    assert isinstance(app_mod._pending_downloads, EphemeralStore)


def test_rejoin_sessions_is_capped(app_mod):
    from slack_channel_export_selfservice_1 import EphemeralStore
    assert isinstance(app_mod._rejoin_sessions, EphemeralStore)


def test_callback_rejects_without_state_cookie(client):
    resp = client.get("/slack/callback?code=fake&state=fake")
    assert resp.status_code == 400
    assert b"Invalid or expired state" in resp.data


def test_rejoin_callback_rejects_state_from_export_flow(client, app_mod):
    # Mint an export-flow cookie and try to use it at the rejoin callback.
    with app_mod.app.app_context():
        nonce, cookie = app_mod.issue_oauth_state("export")
    client.set_cookie("oauth_state", cookie, domain="localhost")
    resp = client.get(f"/slack/rejoin_callback?code=x&state={nonce}")
    assert resp.status_code == 400
    assert b"Invalid or expired state" in resp.data


def test_slack_auth_sets_state_cookie(client):
    resp = client.get("/slack/auth", follow_redirects=False)
    assert resp.status_code == 302
    cookies = resp.headers.getlist("Set-Cookie")
    state_cookies = [c for c in cookies if c.startswith("oauth_state=")]
    assert len(state_cookies) == 1, f"expected one oauth_state cookie, got {cookies!r}"
    header = state_cookies[0]
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header


def test_max_content_length_is_set(app_mod):
    assert app_mod.app.config["MAX_CONTENT_LENGTH"] is not None
    assert app_mod.app.config["MAX_CONTENT_LENGTH"] <= 2 * 1024 * 1024


def test_oversized_upload_rejected(client, app_mod):
    import io
    # Build a "session" so we don't 401 first.
    sid = "test-sid-oversize"
    app_mod._rejoin_sessions.put(sid, {"token": "xoxp-test", "user_id": "U"})
    client.set_cookie(key="rejoin_sid", value=sid, domain="localhost")
    big = b"a,b\n" + (b"x" * (2 * 1024 * 1024))
    resp = client.post(
        "/rejoin/upload",
        data={"csv": (io.BytesIO(big), "big.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code in (400, 413)
