# tests/test_caldav_oauth.py
"""Google CalDAV OAuth: bearer client, token resolution, sync/write-back
threading, and basic-auth diagnostics."""
import pytest

from src import caldav_sync


def test_build_dav_client_bearer_sets_header_and_no_basic_auth():
    """An access_token builds a bearer client: Authorization header on the
    caldav client header set (merged into every request), no basic auth object,
    redirects still disabled."""
    pytest.importorskip("caldav")
    client = caldav_sync._build_dav_client(
        "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
        "me@x.com", "", access_token="ya29.tok",
    )
    assert client.headers.get("Authorization") == "Bearer ya29.tok"
    assert client.auth is None, "bearer client must not carry a basic-auth object"
    assert client.session.max_redirects == 0


def test_build_dav_client_basic_unchanged():
    """No access_token → basic auth path is unchanged (regression guard)."""
    pytest.importorskip("caldav")
    client = caldav_sync._build_dav_client("https://dav.example.com/", "u", "p")
    assert client.session.max_redirects == 0
    assert client.headers.get("Authorization") is None
