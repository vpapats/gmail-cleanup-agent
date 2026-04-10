"""
Local bootstrap script to obtain a Gmail API refresh token.

This script performs the OAuth installed-app flow, launching a browser for you
to grant access to the Gmail API. It saves the resulting credentials to
`.secrets/token.json`. The refresh token from this file should be copied
into your repository’s GitHub secrets as `GOOGLE_REFRESH_TOKEN`.

Usage:

    python -m scripts.gmail_oauth_bootstrap --client-json path/to/client.json

Dependencies:
    pip install google-auth google-auth-oauthlib google-auth-httplib2

The default redirect URI uses the loopback IP. Make sure you have added
`http://127.0.0.1:8765/callback` as an authorised redirect URI in your
Google Cloud OAuth credentials.
"""

import argparse
import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow


def bootstrap(client_json_path: str, token_path: str) -> None:
    client_json_path = os.path.expanduser(client_json_path)
    token_path = os.path.expanduser(token_path)
    token_dir = os.path.dirname(token_path)
    os.makedirs(token_dir, exist_ok=True)

    # Scopes required for Gmail actions and Google Sheets logging
    scopes = [
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/spreadsheets",
    ]

    # Start the installed app flow. Using port 8765 to match the redirect URI.
    flow = InstalledAppFlow.from_client_secrets_file(
        client_json_path,
        scopes=scopes,
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    creds = flow.run_local_server(port=8765)

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

    print(f"Refresh token saved to {token_path}\n")
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
    args = parser.parse_args(argv)
    bootstrap(args.client_json, args.token_path)


if __name__ == "__main__":
    main()
