# tests/test_google_oauth.py
"""Shared Google OAuth2 token-exchange helpers (src/google_oauth.py).

One HTTP round-trip owned in one place; email and CalDAV each keep their own
token persistence. These tests mock httpx so no network call is made.
"""
from unittest import mock

from src import google_oauth


def test_exchange_refresh_token_posts_refresh_grant_and_returns_json():
    resp = mock.MagicMock()
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {"access_token": "ya29.new", "expires_in": 3599}
    with mock.patch("httpx.post", return_value=resp) as post:
        out = google_oauth.exchange_refresh_token("cid", "secret", "1//refresh")
    assert out["access_token"] == "ya29.new"
    args, kwargs = post.call_args
    assert args[0] == google_oauth.GOOGLE_TOKEN_URL
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "1//refresh"


def test_exchange_authorization_code_posts_code_grant():
    resp = mock.MagicMock()
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {"access_token": "ya29.x", "refresh_token": "r", "expires_in": 3600}
    with mock.patch("httpx.post", return_value=resp) as post:
        out = google_oauth.exchange_authorization_code("cid", "secret", "4/code", "https://app/cb")
    assert out["refresh_token"] == "r"
    _, kwargs = post.call_args
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["code"] == "4/code"
    assert kwargs["data"]["redirect_uri"] == "https://app/cb"


def test_google_client_credentials_reads_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "the-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "the-secret")
    assert google_oauth.google_client_credentials() == ("the-id", "the-secret")
