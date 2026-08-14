"""Shared pytest fixtures.

We import the app module under its real name (it has a trailing _1) so tests
exercise the same code path production runs.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SLACK_CLIENT_ID", "test-client-id")
os.environ.setdefault("SLACK_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-cookie-signing")

# Import after env is set so module-level config picks up the test values.
import slack_channel_export_selfservice_1 as app_module  # noqa: E402


@pytest.fixture
def app():
    app_module.app.config["TESTING"] = True
    yield app_module.app
    # Reset module-level EphemeralStores so state doesn't leak between tests.
    for name in ("_rejoin_sessions",):
        store = getattr(app_module, name, None)
        if store is not None and hasattr(store, "_items"):
            with store._lock:
                store._items.clear()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def app_mod():
    """Direct handle to the app module for poking at internals."""
    return app_module


@pytest.fixture
def mock_web_client(mocker):
    """Replace slack_sdk.WebClient inside the app module with a MagicMock factory."""
    instance = MagicMock()
    instance.oauth_v2_access.return_value = {
        "authed_user": {
            "access_token": "xoxp-test",
            "id": "U123",
            "scope": ",".join(app_module.USER_SCOPES),
        }
    }
    instance.users_info.return_value = {"user": {"profile": {"real_name": "Test User"}}}
    instance.users_conversations.return_value = {"channels": [], "response_metadata": {}}
    instance.conversations_open.return_value = {"channel": {"id": "D123"}}
    instance.files_upload_v2.return_value = {"ok": True}
    instance.auth_revoke.return_value = {"ok": True}
    instance.conversations_join.return_value = {"ok": True, "response_metadata": {}}

    factory = MagicMock(return_value=instance)
    mocker.patch.object(app_module, "WebClient", factory)
    return instance
