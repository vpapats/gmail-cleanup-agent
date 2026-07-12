from __future__ import annotations

import os

from src.gmail_client import GmailClient
from src.weekly_auditor import GitHubAuditSource, WeeklyQualityAuditor


def main() -> None:
    source = GitHubAuditSource(
        token=os.getenv("GITHUB_TOKEN", ""),
        repository=os.getenv("GITHUB_REPOSITORY", ""),
    )
    auditor = WeeklyQualityAuditor(
        gmail=GmailClient(),
        source=source,
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        model=os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite"),
    )
    print("Weekly audit complete:", auditor.run())


if __name__ == "__main__":
    main()

