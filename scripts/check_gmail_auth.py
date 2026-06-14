from __future__ import annotations

import sys

from google.auth.exceptions import RefreshError

from src.auth import build_credentials


def main() -> int:
    try:
        creds = build_credentials()
    except RuntimeError as exc:
        print(f"::error::{exc}")
        return 1
    except RefreshError as exc:
        print("::error::Google rejected the stored Gmail refresh token.")
        print("Refresh GOOGLE_REFRESH_TOKEN with scripts/gmail_oauth_bootstrap.py, then update the repository secret.")
        print(f"Google response: {exc}")
        return 1

    if not creds.valid:
        print("::error::Gmail credentials refreshed but are not valid.")
        return 1

    print("Gmail credentials refreshed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
