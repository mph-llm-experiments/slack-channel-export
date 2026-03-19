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
