from slack_channel_export_selfservice_1 import (
    app,
    consume_oauth_state,
    issue_oauth_state,
)


def test_issue_returns_distinct_nonces():
    with app.app_context():
        n1, _ = issue_oauth_state("export")
        n2, _ = issue_oauth_state("export")
    assert n1 != n2


def test_consume_accepts_matching_nonce_and_flow():
    with app.app_context():
        nonce, cookie = issue_oauth_state("export")
        assert consume_oauth_state("export", nonce, cookie) is True


def test_consume_rejects_wrong_flow():
    with app.app_context():
        nonce, cookie = issue_oauth_state("export")
        assert consume_oauth_state("rejoin", nonce, cookie) is False


def test_consume_rejects_missing_cookie():
    assert consume_oauth_state("export", "abc", None) is False


def test_consume_rejects_missing_state():
    with app.app_context():
        _, cookie = issue_oauth_state("export")
        assert consume_oauth_state("export", None, cookie) is False


def test_consume_rejects_tampered_cookie():
    with app.app_context():
        nonce, cookie = issue_oauth_state("export")
        tampered = cookie[:-2] + ("AA" if cookie[-2:] != "AA" else "BB")
        assert consume_oauth_state("export", nonce, tampered) is False


def test_consume_rejects_nonce_mismatch():
    with app.app_context():
        _, cookie = issue_oauth_state("export")
        assert consume_oauth_state("export", "different-nonce", cookie) is False


def test_consume_rejects_garbage_cookie():
    # Random string that won't even pass itsdangerous's base64/separator parsing.
    assert consume_oauth_state("export", "anything", "not-a-real-token") is False
