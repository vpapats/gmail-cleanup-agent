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

from google_auth_oauthlib.flow import InstalledAppFlow


def bootstrap(client_json_path: str, token_path: str, no_browser: bool = False) -> None:
    client_json_path = os.path.expanduser(client_json_path)
    token_path = os.path.expanduser(token_path)
    token_dir = os.path.dirname(token_path)
    os.makedirs(token_dir, exist_ok=True)

    scopes = [
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/spreadsheets",
    ]

    flow = InstalledAppFlow.from_client_secrets_file(
        client_json_path,
        scopes=scopes,
        redirect_uri="http://127.0.0.1:8765/callback",
    )

    if no_browser:
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        print("Open this URL and approve access:\n")
        print(auth_url)
        print(
            "\nAfter approval, copy the full redirected URL from your browser "
            "(it starts with http://127.0.0.1:8765/callback?code=...)"
        )
        redirected_url = input("Paste redirected URL here: ").strip()
        flow.fetch_token(authorization_response=redirected_url)
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
