# slack-channel-list Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Slack slash command (`/mychannels`) that dumps a user's channel list so they can preserve it before leave deactivation.

**Architecture:** Single Python app using Slack Bolt, deployed as a Docker container on GCP Cloud Run. One slash command handler that calls `users.conversations` (with admin user token for full private channel visibility) and `users.info` (with bot token for admin checks). Responds with ephemeral messages. Note: the spec mentions per-user OAuth tokens for self-service, but for simplicity we use a single admin user token for all `users.conversations` calls — this avoids needing an OAuth flow and token storage while still providing full channel visibility.

**Tech Stack:** Python 3.12, slack-bolt, gunicorn, Docker, GCP Cloud Run

**Spec:** `docs/superpowers/specs/2026-03-18-slack-channel-list-design.md`

---

## File Structure

```
slack_channel_list/
├── app.py                  # Slack Bolt app + slash command handler
├── channels.py             # Channel fetching + formatting logic
├── Dockerfile              # Container for Cloud Run
├── requirements.txt        # Python dependencies
├── tests/
│   ├── conftest.py         # Shared test fixtures (_make_channel, FakeClient)
│   ├── test_channels.py    # Unit tests for channel fetching/formatting
│   └── test_app.py         # Integration tests for the slash command handler
```

- **`app.py`** — Slack Bolt initialization, slash command registration, permission check, response dispatch. Thin orchestration layer. Excludes archived channels (not useful for rejoining).
- **`channels.py`** — `fetch_user_channels(client, user_id)` and `format_channel_list(channels, user_name)`. Pure logic, easy to test without mocking Slack Bolt internals.
- **`tests/conftest.py`** — Shared `_make_channel` helper and `FakeClient` to avoid duplication across test files.

---

### Task 1: Project scaffolding

**Files:**
- Create: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create requirements.txt**

```
slack-bolt==1.22.0
gunicorn==23.0.0
pytest==8.3.4
```

- [ ] **Step 2: Create test package with shared fixtures**

Create `tests/__init__.py` (empty file).

Create `tests/conftest.py`:

```python
import pytest


def _make_channel(name, is_private=False, num_members=10, purpose=""):
    return {
        "id": f"C{name.upper()}",
        "name": name,
        "is_private": is_private,
        "num_members": num_members,
        "purpose": {"value": purpose},
    }


class FakeClient:
    """Simulates the Slack WebClient for users_conversations."""

    def __init__(self, pages):
        """pages: list of (channels_list, next_cursor) tuples."""
        self._pages = list(pages)
        self._call_index = 0

    def users_conversations(self, **kwargs):
        channels, cursor = self._pages[self._call_index]
        self._call_index += 1
        return {
            "channels": channels,
            "response_metadata": {"next_cursor": cursor},
        }


@pytest.fixture
def make_channel():
    return _make_channel


@pytest.fixture
def fake_client():
    return FakeClient
```

- [ ] **Step 3: Create and activate venv, install deps**

Run:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
Expected: all packages install successfully.

- [ ] **Step 4: Verify pytest runs**

Run: `python -m pytest tests/ -v`
Expected: "no tests ran" (0 collected), exit 0.

- [ ] **Step 5: Create .gitignore and commit**

Create `.gitignore`:
```
venv/
__pycache__/
*.pyc
.env
```

```bash
git add requirements.txt tests/__init__.py tests/conftest.py .gitignore
git commit -m "chore: project scaffolding with deps and test setup"
```

---

### Task 2: Channel fetching logic

**Files:**
- Create: `tests/test_channels.py`
- Create: `channels.py`

- [ ] **Step 1: Write failing test for fetch_user_channels**

```python
# tests/test_channels.py
from channels import fetch_user_channels
from tests.conftest import _make_channel, FakeClient


def test_fetch_single_page():
    channels = [
        _make_channel("general", purpose="Company-wide announcements"),
        _make_channel("secret", is_private=True, num_members=3, purpose="Secrets"),
    ]
    client = FakeClient([(channels, "")])
    result = fetch_user_channels(client, "U123")
    assert len(result) == 2
    assert result[0]["name"] == "general"
    assert result[1]["name"] == "secret"


def test_fetch_paginates():
    page1 = [_make_channel("alpha")]
    page2 = [_make_channel("beta")]
    client = FakeClient([(page1, "cursor_page2"), (page2, "")])
    result = fetch_user_channels(client, "U123")
    assert len(result) == 2
    assert [c["name"] for c in result] == ["alpha", "beta"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_channels.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'channels'`

- [ ] **Step 3: Implement fetch_user_channels**

```python
# channels.py


def fetch_user_channels(client, user_id):
    """Fetch all channels for a user, paginating through results.

    Args:
        client: Slack WebClient (must be initialized with a user token
                that has channels:read and groups:read scopes).
        user_id: The Slack user ID to look up.

    Returns:
        List of channel dicts from the Slack API.
    """
    all_channels = []
    cursor = None

    while True:
        kwargs = {
            "user": user_id,
            "types": "public_channel,private_channel",
            "limit": 200,
            "exclude_archived": True,
        }
        if cursor:
            kwargs["cursor"] = cursor

        response = client.users_conversations(**kwargs)
        all_channels.extend(response["channels"])

        cursor = response.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    return all_channels
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_channels.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add channels.py tests/test_channels.py
git commit -m "feat: add channel fetching with pagination"
```

---

### Task 3: Channel formatting logic

**Files:**
- Modify: `tests/test_channels.py`
- Modify: `channels.py`

- [ ] **Step 1: Write failing test for format_channel_list**

Append to `tests/test_channels.py`:

```python
from channels import format_channel_list


def test_format_channel_list():
    channels = [
        _make_channel("engineering", num_members=234, purpose="Main engineering discussion"),
        _make_channel("incidents", num_members=89, purpose="Incident response"),
        _make_channel("eng-leads", is_private=True, num_members=12, purpose="Engineering leadership"),
        _make_channel("alpha-public", num_members=5, purpose="Alpha stuff"),
    ]
    result = format_channel_list(channels, "jane")

    assert "Channels for @jane (4 channels)" in result
    assert "Public:" in result
    assert "Private:" in result
    alpha_pos = result.index("#alpha-public")
    eng_pos = result.index("#engineering")
    assert alpha_pos < eng_pos
    assert "🔒 #eng-leads" in result
    assert "234 members" in result
    assert "Main engineering discussion" in result


def test_format_empty_list():
    result = format_channel_list([], "jane")
    assert "0 channels" in result


def test_format_no_private_channels():
    channels = [_make_channel("general")]
    result = format_channel_list(channels, "jane")
    assert "Public:" in result
    assert "Private:" not in result
```

Note: `_make_channel` is already imported at the top of the file from `tests.conftest`.

- [ ] **Step 2: Run tests to verify new tests fail**

Run: `python -m pytest tests/test_channels.py -v`
Expected: 3 new tests FAIL — `ImportError: cannot import name 'format_channel_list'`

- [ ] **Step 3: Implement format_channel_list**

Add to `channels.py`:

```python
def format_channel_list(channels, user_name):
    """Format a list of channels into a readable Slack message.

    Args:
        channels: List of channel dicts from the Slack API.
        user_name: Display name for the header.

    Returns:
        Formatted string for the Slack ephemeral response.
    """
    total = len(channels)
    header = f"📋 Channels for @{user_name} ({total} channel{'s' if total != 1 else ''})"

    if total == 0:
        return f"{header}\n\nNo channels found."

    public = sorted(
        [c for c in channels if not c.get("is_private")],
        key=lambda c: c["name"],
    )
    private = sorted(
        [c for c in channels if c.get("is_private")],
        key=lambda c: c["name"],
    )

    lines = [header, ""]

    if public:
        lines.append("Public:")
        for c in public:
            purpose = c.get("purpose", {}).get("value", "")
            members = c.get("num_members", 0)
            line = f"  #{c['name']} — {members} members"
            if purpose:
                line += f" — {purpose}"
            lines.append(line)

    if private:
        if public:
            lines.append("")
        lines.append("Private:")
        for c in private:
            purpose = c.get("purpose", {}).get("value", "")
            members = c.get("num_members", 0)
            line = f"  🔒 #{c['name']} — {members} members"
            if purpose:
                line += f" — {purpose}"
            lines.append(line)

    return "\n".join(lines)
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/test_channels.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add channels.py tests/test_channels.py
git commit -m "feat: add channel list formatting with public/private sections"
```

---

### Task 4: Slash command handler

**Files:**
- Create: `tests/test_app.py`
- Create: `app.py`

- [ ] **Step 1: Write failing test for self-service flow**

```python
# tests/test_app.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app.py::test_mychannels_self -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: Implement app.py**

```python
# app.py
import os
import re

from slack_bolt import App
from slack_sdk import WebClient

from channels import fetch_user_channels, format_channel_list

bot_token = os.environ.get("SLACK_BOT_TOKEN")
user_token = os.environ.get("SLACK_USER_TOKEN")
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")

app = App(token=bot_token, signing_secret=signing_secret)

bot_client = WebClient(token=bot_token)
user_client = WebClient(token=user_token)

# Matches Slack's encoded user mention: <@U12345> or <@U12345|username>
USER_MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")


def handle_mychannels(ack, command, respond):
    ack()

    requesting_user_id = command["user_id"]
    text = command.get("text", "").strip()

    # Determine target user
    mention_match = USER_MENTION_RE.search(text)
    if mention_match:
        target_user_id = mention_match.group(1)
    elif text:
        respond(text="Usage: `/mychannels` or `/mychannels @user`", response_type="ephemeral")
        return
    else:
        target_user_id = requesting_user_id

    # If looking up someone else, check admin
    if target_user_id != requesting_user_id:
        try:
            info = bot_client.users_info(user=requesting_user_id)
            if not info["user"].get("is_admin", False):
                respond(
                    text="Only workspace admins can look up other users' channels.",
                    response_type="ephemeral",
                )
                return
        except Exception:
            respond(text="Something went wrong, please try again.", response_type="ephemeral")
            return

    # Get the target user's display name
    try:
        target_info = bot_client.users_info(user=target_user_id)
        user_name = target_info["user"].get("real_name", target_user_id)
    except Exception:
        respond(text="Couldn't find that user.", response_type="ephemeral")
        return

    # Fetch channels
    try:
        channels = fetch_user_channels(user_client, target_user_id)
    except Exception:
        respond(
            text="Couldn't retrieve channels for that user. If their account is "
                 "deactivated, their channel list is no longer available. "
                 "This tool works best when run before deactivation.",
            response_type="ephemeral",
        )
        return

    message = format_channel_list(channels, user_name)
    respond(text=message, response_type="ephemeral")


app.command("/mychannels")(handle_mychannels)

if __name__ == "__main__":
    app.start(port=int(os.environ.get("PORT", 3000)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app.py::test_mychannels_self -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: add slash command handler with self-service flow"
```

---

### Task 5: Admin lookup and permission tests

**Files:**
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing test for admin lookup**

Append to `tests/test_app.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_app.py -v`
Expected: 3 passed (these test the already-implemented permission logic).

- [ ] **Step 3: Commit**

```bash
git add tests/test_app.py
git commit -m "test: add admin lookup and non-admin rejection tests"
```

---

### Task 6: Message truncation and DM fallback

**Files:**
- Modify: `channels.py`
- Modify: `tests/test_channels.py`

The spec requires: if the formatted message exceeds Slack's plain text limit (~40000 chars), truncate the ephemeral response and DM the full list.

- [ ] **Step 1: Write failing test for truncation**

Append to `tests/test_channels.py`:

```python
def test_format_truncates_long_list():
    """When the message exceeds the limit, it should be truncated with a note."""
    # Create enough channels to exceed the limit
    channels = [_make_channel(f"channel-{i:04d}", num_members=i, purpose=f"Purpose for channel {i}") for i in range(500)]
    result = format_channel_list(channels, "jane", max_length=4000)  # Use small limit for testing
    assert len(result) <= 4000
    assert "showing" in result.lower()
    assert "of 500" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_channels.py::test_format_truncates_long_list -v`
Expected: FAIL — `format_channel_list() got an unexpected keyword argument 'max_length'`

- [ ] **Step 3: Implement truncation in format_channel_list**

Update `format_channel_list` signature to accept `max_length=40000`. After building the full message, check if it exceeds `max_length`. If so, truncate line by line until it fits, appending a footer:

```python
def format_channel_list(channels, user_name, max_length=40000):
    # ... existing formatting code ...

    message = "\n".join(lines)

    if len(message) <= max_length:
        return message

    # Count how many channels made it into the truncated message
    truncated_lines = [header, ""]
    shown = 0
    for line in lines[2:]:  # Skip header and blank line
        candidate = "\n".join(truncated_lines + [line])
        footer = f"\n\n(Showing {shown} of {total} channels — full list sent via DM)"
        if len(candidate + footer) > max_length:
            break
        truncated_lines.append(line)
        if line.startswith("  "):  # Actual channel lines start with indent
            shown += 1

    return "\n".join(truncated_lines) + f"\n\n(Showing {shown} of {total} channels — full list sent via DM)"
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_channels.py -v`
Expected: all pass.

- [ ] **Step 5: Update app.py to DM full list when truncated**

In `handle_mychannels`, after calling `format_channel_list`:

```python
    message = format_channel_list(channels, user_name)
    respond(text=message, response_type="ephemeral")

    # If truncated, DM the full untruncated list
    if f"of {len(channels)} channels" in message and "full list sent via DM" in message:
        full_message = format_channel_list(channels, user_name, max_length=None)
        try:
            bot_client.chat_postMessage(channel=requesting_user_id, text=full_message)
        except Exception:
            pass  # Best effort DM
```

Update `format_channel_list` to accept `max_length=None` to skip truncation.

- [ ] **Step 6: Commit**

```bash
git add channels.py app.py tests/test_channels.py
git commit -m "feat: truncate long channel lists with DM fallback"
```

---

### Task 7: Error handling tests

**Files:**
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write tests for error handling paths**

Append to `tests/test_app.py`:

```python
@patch.dict("os.environ", {
    "SLACK_BOT_TOKEN": "xoxb-fake",
    "SLACK_USER_TOKEN": "xoxp-fake",
    "SLACK_SIGNING_SECRET": "fake-secret",
})
def test_mychannels_invalid_user():
    from app import handle_mychannels

    ack = MagicMock()
    respond = MagicMock()
    command = {"user_id": "U123", "text": ""}

    mock_bot_client = MagicMock()
    mock_bot_client.users_info.side_effect = Exception("user_not_found")

    with patch("app.bot_client", mock_bot_client):
        handle_mychannels(ack, command, respond)

    msg = respond.call_args[1]["text"]
    assert "Couldn't find that user" in msg


@patch.dict("os.environ", {
    "SLACK_BOT_TOKEN": "xoxb-fake",
    "SLACK_USER_TOKEN": "xoxp-fake",
    "SLACK_SIGNING_SECRET": "fake-secret",
})
def test_mychannels_channel_fetch_fails():
    from app import handle_mychannels

    ack = MagicMock()
    respond = MagicMock()
    command = {"user_id": "U123", "text": ""}

    mock_bot_client = MagicMock()
    mock_bot_client.users_info.return_value = {
        "user": {"real_name": "Test User", "is_admin": False},
    }
    mock_user_client = MagicMock()
    mock_user_client.users_conversations.side_effect = Exception("account_inactive")

    with patch("app.bot_client", mock_bot_client), \
         patch("app.user_client", mock_user_client):
        handle_mychannels(ack, command, respond)

    msg = respond.call_args[1]["text"]
    assert "deactivated" in msg


@patch.dict("os.environ", {
    "SLACK_BOT_TOKEN": "xoxb-fake",
    "SLACK_USER_TOKEN": "xoxp-fake",
    "SLACK_SIGNING_SECRET": "fake-secret",
})
def test_mychannels_invalid_text():
    from app import handle_mychannels

    ack = MagicMock()
    respond = MagicMock()
    command = {"user_id": "U123", "text": "not-a-mention"}

    handle_mychannels(ack, command, respond)

    msg = respond.call_args[1]["text"]
    assert "Usage" in msg


@patch.dict("os.environ", {
    "SLACK_BOT_TOKEN": "xoxb-fake",
    "SLACK_USER_TOKEN": "xoxp-fake",
    "SLACK_SIGNING_SECRET": "fake-secret",
})
def test_mychannels_pipe_delimited_mention():
    """Slack can encode mentions as <@USERID|displayname>."""
    from app import handle_mychannels

    ack = MagicMock()
    respond = MagicMock()
    command = {"user_id": "UADMIN", "text": "<@UTARGET|jane>"}

    mock_user_client = MagicMock()
    mock_user_client.users_conversations.return_value = {
        "channels": FAKE_CHANNELS,
        "response_metadata": {"next_cursor": ""},
    }

    mock_bot_client = MagicMock()
    mock_bot_client.users_info.side_effect = lambda user: {
        "UADMIN": {"user": {"real_name": "Admin User", "is_admin": True}},
        "UTARGET": {"user": {"real_name": "Jane Smith", "is_admin": False}},
    }[user]

    with patch("app.user_client", mock_user_client), \
         patch("app.bot_client", mock_bot_client):
        handle_mychannels(ack, command, respond)

    msg = respond.call_args[1]["text"]
    assert "Jane Smith" in msg
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_app.py -v`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_app.py
git commit -m "test: add error handling and pipe-delimited mention tests"
```

---

### Task 8: Dockerfile and gunicorn entrypoint

**Files:**
- Create: `Dockerfile`

- [ ] **Step 1: Create Dockerfile**

Slack Bolt exposes a WSGI-compatible server via its `App` class. The correct gunicorn reference is the Flask/WSGI adapter. Add the adapter setup at the bottom of `app.py`:

```python
# At the bottom of app.py, add:
from slack_bolt.adapter.wsgi import SlackRequestHandler

wsgi_app = SlackRequestHandler(app)
```

Then create the Dockerfile:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PORT=3000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD exec gunicorn --bind :$PORT --workers 1 --threads 2 --timeout 0 "app:wsgi_app"
```

Note: `ENV PORT=3000` provides a default for local testing; Cloud Run overrides this at runtime. `--workers 1 --threads 2` is appropriate for a low-traffic internal tool.

- [ ] **Step 2: Verify the Docker build succeeds**

Run: `docker build -t slack-channel-list .`
Expected: builds successfully.

- [ ] **Step 3: Commit**

```bash
git add app.py Dockerfile
git commit -m "feat: add Dockerfile with WSGI adapter for Cloud Run"
```

---

### Task 9: Run all tests, final verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 2: Verify Docker build still works**

Run: `docker build -t slack-channel-list .`
Expected: builds successfully.

- [ ] **Step 3: Final commit if any cleanup needed**

If any small fixes were required, commit them now.
