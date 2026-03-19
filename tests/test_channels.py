from channels import fetch_user_channels, format_channel_list
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
