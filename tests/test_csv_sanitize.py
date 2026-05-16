import csv
import io

from slack_channel_export_selfservice_1 import _sanitize_csv_cell


def test_leaves_normal_strings_alone():
    assert _sanitize_csv_cell("hello") == "hello"
    assert _sanitize_csv_cell("") == ""
    assert _sanitize_csv_cell("Team Sync") == "Team Sync"


def test_escapes_equals_prefix():
    assert _sanitize_csv_cell("=SUM(A1)") == "'=SUM(A1)"


def test_escapes_plus_minus_at():
    assert _sanitize_csv_cell("+1") == "'+1"
    assert _sanitize_csv_cell("-1") == "'-1"
    assert _sanitize_csv_cell("@foo") == "'@foo"


def test_escapes_tab_and_cr():
    assert _sanitize_csv_cell("\tfoo") == "'\tfoo"
    assert _sanitize_csv_cell("\rfoo") == "'\rfoo"


def test_passes_through_non_strings():
    assert _sanitize_csv_cell(42) == 42
    assert _sanitize_csv_cell(True) is True
    assert _sanitize_csv_cell(None) is None


def test_writerow_round_trip_with_sanitizer():
    row = {
        "type": "Public Channel",
        "name": "team-sync",
        "channel_id": "C123",
        "is_archived": False,
        "num_members": 12,
        "purpose": "=cmd|'/c calc'!A0",
    }
    sanitized = {k: _sanitize_csv_cell(v) for k, v in row.items()}
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(row.keys()))
    writer.writeheader()
    writer.writerow(sanitized)
    out = buf.getvalue()
    assert "'=cmd" in out
    assert ",=cmd" not in out
