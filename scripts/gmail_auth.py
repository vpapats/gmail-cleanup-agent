"""
Helper for loading Gmail API credentials and building a Gmail client.

This module reads the client ID/secret and refresh token from environment
variables and uses `google.oauth2.credentials.Credentials` to create an
authorised Gmail service.  The `gmail.modify` scope is used to allow
label modifications and moving threads to Trash.
"""

from __future__ import annotations

import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Gmail scope allowing full access.  See https://developers.google.com/gmail/api/auth/scopes
GMAIL_SCOPE = "https://mail.google.com/"


def get_credentials() -> Credentials:
    """Load credentials from environment variables.

    Expects the following environment variables to be set:

    - GOOGLE_CLIENT_ID
    - GOOGLE_CLIENT_SECRET
    - GOOGLE_REFRESH_TOKEN

    Returns a `google.oauth2.credentials.Credentials` object that can be
    used with the Gmail API.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError(
            "Missing required environment variables: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN"
        )

    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[GMAIL_SCOPE],
    )
    # Ensure token is refreshed before use
    creds.refresh(Request())
    return creds


def build_gmail_service(creds: Optional[Credentials] = None):
    """Construct a Gmail API client using provided credentials.

    Args:
        creds: Optional preloaded credentials.  If `None`, credentials will be
            loaded from environment variables via `get_credentials()`.

    Returns:
        A resource object with Gmail API methods.
    """
    if creds is None:
        creds = get_credentials()
    # The discovery service for Gmail v1
    return build("gmail", "v1", credentials=creds)
