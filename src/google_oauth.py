# src/google_oauth.py
"""Shared Google OAuth2 token-exchange helpers.

The email integration (tokens on the EmailAccount row) and the CalDAV
integration (tokens in user prefs) both refresh Google access tokens against
the same endpoint. This module owns the single HTTP round-trip; each caller
keeps its own persistence. Deliberately stdlib + httpx only (no FastAPI / DB
imports) so src/caldav_sync.py can import it without a circular dependency.
"""
from __future__ import annotations

import os

import httpx

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def google_client_credentials() -> tuple[str, str]:
    """The Google OAuth client id/secret from env (shared with the Gmail flow)."""
    return (
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    )


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
