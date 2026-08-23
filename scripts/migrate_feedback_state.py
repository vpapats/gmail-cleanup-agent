from __future__ import annotations

from src.gmail_client import GmailClient
from src.github_state import GitHubFeedbackStateStore


def migrate_feedback_state(
    gmail: GmailClient,
    feedback_state: GitHubFeedbackStateStore,
) -> tuple[int, int, bool]:
    legacy_ids = gmail.load_legacy_feedback_message_ids()
    existing_ids = feedback_state.load()
    merged_ids = list(dict.fromkeys([*existing_ids, *legacy_ids]))

    if legacy_ids:
        feedback_state.save(merged_ids)
        deleted = gmail.delete_legacy_feedback_draft()
        if not deleted:
            raise RuntimeError("Legacy correction memory draft disappeared before verified deletion")
    else:
        deleted = False

    return len(legacy_ids), len(merged_ids), deleted


def main() -> None:
    legacy_count, total_count, deleted = migrate_feedback_state(
        GmailClient(),
        GitHubFeedbackStateStore.from_env(),
    )
    print(
        "Feedback state migration complete:",
        {
            "legacy_ids_migrated": legacy_count,
            "encrypted_state_ids": total_count,
            "legacy_draft_deleted": deleted,
        },
    )


if __name__ == "__main__":
    main()
