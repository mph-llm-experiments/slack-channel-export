from unittest.mock import MagicMock, patch
from tests.conftest import _make_channel


FAKE_CHANNELS = [
    _make_channel("general", num_members=100, purpose="Company chat"),
    _make_channel("secret", is_private=True, num_members=5, purpose="Hush"),
]


@patch.dict("os.environ", {
    "SLACK_BOT_TOKEN": "xoxb-fake",
    "SLACK_USER_TOKEN": "xoxp-fake",
    "SLACK_SIGNING_SECRET": "fake-secret",
})
def test_mychannels_self():
    from app import handle_mychannels

    ack = MagicMock()
    respond = MagicMock()
    command = {"user_id": "U123", "text": ""}

    mock_user_client = MagicMock()
    mock_user_client.users_conversations.return_value = {
        "channels": FAKE_CHANNELS,
        "response_metadata": {"next_cursor": ""},
    }

    mock_bot_client = MagicMock()
    mock_bot_client.users_info.return_value = {
        "user": {"real_name": "Test User", "is_admin": False},
    }

    with patch("app.user_client", mock_user_client), \
         patch("app.bot_client", mock_bot_client):
        handle_mychannels(ack, command, respond)

    ack.assert_called_once()
    respond.assert_called_once()
    msg = respond.call_args[1]["text"]
    assert "Test User" in msg
    assert "#general" in msg
    assert "🔒 #secret" in msg


@patch.dict("os.environ", {
    "SLACK_BOT_TOKEN": "xoxb-fake",
    "SLACK_USER_TOKEN": "xoxp-fake",
    "SLACK_SIGNING_SECRET": "fake-secret",
})
def test_mychannels_admin_lookup():
    from app import handle_mychannels

    ack = MagicMock()
    respond = MagicMock()
    command = {"user_id": "UADMIN", "text": "<@UTARGET>"}

    mock_user_client = MagicMock()
    mock_user_client.users_conversations.return_value = {
        "channels": FAKE_CHANNELS,
        "response_metadata": {"next_cursor": ""},
    }

    mock_bot_client = MagicMock()
    mock_bot_client.users_info.side_effect = lambda user: {
        "UADMIN": {"user": {"real_name": "Admin User", "is_admin": True}},
        "UTARGET": {"user": {"real_name": "Target User", "is_admin": False}},
    }[user]

    with patch("app.user_client", mock_user_client), \
         patch("app.bot_client", mock_bot_client):
        handle_mychannels(ack, command, respond)

    msg = respond.call_args[1]["text"]
    assert "Target User" in msg
    assert "#general" in msg


@patch.dict("os.environ", {
    "SLACK_BOT_TOKEN": "xoxb-fake",
    "SLACK_USER_TOKEN": "xoxp-fake",
    "SLACK_SIGNING_SECRET": "fake-secret",
})
def test_mychannels_non_admin_blocked():
    from app import handle_mychannels

    ack = MagicMock()
    respond = MagicMock()
    command = {"user_id": "UREGULAR", "text": "<@UTARGET>"}

    mock_bot_client = MagicMock()
    mock_bot_client.users_info.return_value = {
        "user": {"real_name": "Regular User", "is_admin": False},
    }

    with patch("app.bot_client", mock_bot_client):
        handle_mychannels(ack, command, respond)

    msg = respond.call_args[1]["text"]
    assert "Only workspace admins" in msg
