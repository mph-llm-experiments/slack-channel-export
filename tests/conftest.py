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
