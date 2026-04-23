"""
Local bootstrap script to obtain a Gmail API refresh token.

This script performs the OAuth installed-app flow and saves resulting credentials
in `.secrets/token.json`.

Typical usage (local browser flow):

    python scripts/gmail_oauth_bootstrap.py --client-json path/to/client.json

Headless/Colab usage (manual link flow):

    python scripts/gmail_oauth_bootstrap.py --client-json path/to/client.json --no-browser

For `--no-browser`, open the printed URL, approve access, and paste the final
redirect URL (the localhost URL shown after authorization) back into the prompt.
"""

import argparse
import json
import os
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow


REDIRECT_URI = "http://127.0.0.1:8765/callback"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]


def _extract_code(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        parsed = urlparse(cleaned)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            raise ValueError("Redirect URL did not include a code parameter.")
        return code
    return cleaned


def bootstrap(client_json_path: str, token_path: str, no_browser: bool = False) -> None:
    client_json_path = os.path.expanduser(client_json_path)
    token_path = os.path.expanduser(token_path)
    token_dir = os.path.dirname(token_path)
    os.makedirs(token_dir, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(
        client_json_path,
        scopes=GMAIL_SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    if no_browser:
        # Explicitly set redirect_uri to ensure it is present in the auth URL
        # in headless notebook environments (e.g. Colab).
        flow.redirect_uri = REDIRECT_URI
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            redirect_uri=REDIRECT_URI,
        )
        print("Open this URL and approve access:\n")
        print(auth_url)
        print(
            "\nAfter approval, copy the full redirected URL from your browser "
            f"(it starts with {REDIRECT_URI}?code=...)"
        )
        pasted_value = input("Paste authorization code or redirected URL here: ")
        flow.fetch_token(code=_extract_code(pasted_value))
        creds = flow.credentials
    else:
        creds = flow.run_local_server(port=8765, open_browser=True)

    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

    with open(token_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"\nRefresh token saved to {token_path}")
    print("IMPORTANT: Do not commit this file to version control.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Bootstrap Gmail OAuth refresh token")
    parser.add_argument(
        "--client-json",
        required=True,
        help="Path to OAuth client JSON downloaded from Google Cloud Console",
    )
    parser.add_argument(
        "--token-path",
        default=".secrets/token.json",
        help="Where to store the generated token JSON",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Use manual copy/paste auth flow (useful for Colab/headless environments)",
    )
    args = parser.parse_args(argv)
    bootstrap(args.client_json, args.token_path, no_browser=args.no_browser)


if __name__ == "__main__":
    main()
