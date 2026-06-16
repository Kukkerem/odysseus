# src/google_oauth.py
"""Shared Google OAuth2 token-exchange helpers.

The email integration (tokens on the EmailAccount row) and the CalDAV
integration (tokens in user prefs) both refresh Google access tokens against
the same endpoint. This module owns the single HTTP round-trip; each caller
keeps its own persistence. Deliberately stdlib + httpx only (no FastAPI / DB
imports) so src/caldav_sync.py can import it without a circular dependency.
"""
from __future__ import annotations

import json
import os

import httpx

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def google_client_credentials() -> tuple[str, str]:
    """The Google OAuth client id/secret from env (shared with the Gmail flow)."""
    return (
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    )


def parse_client_json(text: str) -> tuple[str, str]:
    """Extract (client_id, client_secret) from Google's downloaded
    ``client_secret_*.json``. Accepts the ``web`` / ``installed`` wrapper
    shapes and a flat ``{"client_id", "client_secret"}`` object.
    Raises ValueError when the input is not valid JSON or either field is
    missing."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        raise ValueError("not valid JSON") from e
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    inner = data.get("web") or data.get("installed") or data
    if not isinstance(inner, dict):
        raise ValueError("unexpected client JSON shape")
    client_id = (inner.get("client_id") or "").strip()
    client_secret = (inner.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("missing client_id or client_secret")
    return client_id, client_secret


def exchange_refresh_token(client_id: str, client_secret: str, refresh_token: str,
                           timeout: float = 10) -> dict:
    """Exchange a refresh token for a fresh access token.

    Returns the parsed token JSON ({"access_token", "expires_in", ...}).
    Raises httpx.HTTPError on a non-2xx response or transport failure.
    """
    resp = httpx.post(GOOGLE_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def exchange_authorization_code(client_id: str, client_secret: str, code: str,
                                redirect_uri: str, timeout: float = 10) -> dict:
    """Exchange a consent-flow authorization code for tokens.

    Returns the parsed token JSON ({"access_token", "refresh_token",
    "expires_in", ...}). Raises httpx.HTTPError on failure.
    """
    resp = httpx.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
