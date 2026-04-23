"""Validate Gmail OAuth credentials by listing labels."""

from scripts.gmail_auth import build_gmail_service


def main() -> None:
    labels = build_gmail_service().users().labels().list(userId="me").execute().get("labels", [])
    print(f"Retrieved {len(labels)} labels")


if __name__ == "__main__":
    main()
