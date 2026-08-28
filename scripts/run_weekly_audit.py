from __future__ import annotations

import os

from src.gmail_client import GmailClient
from src.weekly_auditor import GitHubAuditSource, WeeklyQualityAuditor
from src.weekly_review import GitHubReviewPublisher


def main() -> None:
    source = GitHubAuditSource(
        token=os.getenv("GITHUB_TOKEN", ""),
        repository=os.getenv("GITHUB_REPOSITORY", ""),
    )
    publisher = GitHubReviewPublisher(
        token=os.getenv("GITHUB_TOKEN", ""),
        repository=os.getenv("GITHUB_REPOSITORY", ""),
        encryption_key=os.getenv("GMAIL_FOMO_STATE_KEY", ""),
        base_branch=os.getenv("GMAIL_FOMO_REVIEW_BASE_BRANCH", "main"),
        create_pull_request=os.getenv("GMAIL_FOMO_CREATE_REVIEW_PR", "").lower()
        in {"1", "true", "yes"},
    )
    auditor = WeeklyQualityAuditor(
        gmail=GmailClient(),
        source=source,
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        model=os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite"),
        review_publisher=publisher,
        review_confirm_url=os.getenv("WEEKLY_REVIEW_APP_URL", ""),
        review_approval_secret=os.getenv("WEEKLY_REVIEW_APPROVAL_SECRET", ""),
    )
    print("Weekly audit complete:", auditor.run())


if __name__ == "__main__":
    main()
