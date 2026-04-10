"""
Validate Gmail API credentials by listing the user’s labels.

This script is intended to be run in a GitHub Actions workflow to verify that
the OAuth client ID, client secret and refresh token are correct.  It uses
the helper in `gmail_auth.py` to build a Gmail API client and prints the
labels in the authenticated account.  If the call succeeds, the workflow
completes; otherwise it fails with an error.
"""

import json
import os
from pprint import pprint

from gmail_auth import build_gmail_service


def main() -> None:
    service = build_gmail_service()
    # Retrieve the list of labels in the user’s mailbox
    results = service.users().labels().list(userId="me").execute()
    labels = results.get("labels", [])
    print("Retrieved labels:")
    for label in labels:
        print(f" - {label['name']}")


if __name__ == "__main__":
    main()
