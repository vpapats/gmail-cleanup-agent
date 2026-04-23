"""Legacy helper kept for compatibility.

Prefer `src.auth.build_credentials` and `src.gmail_client.GmailClient`.
"""

from src.auth import build_credentials
from googleapiclient.discovery import build


def build_gmail_service():
    return build("gmail", "v1", credentials=build_credentials(), cache_discovery=False)
