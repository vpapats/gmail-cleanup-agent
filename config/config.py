"""
Configuration for the Gmail cleanup agent.

This module contains constants and settings that control the agent’s behaviour.  Adjust
these values to fit your workflow.  Protected domains and senders should be
populated with the addresses you never want the agent to trash.  `TRASH_LANE_SENDERS`
should be populated with explicit newsletter senders you trust the agent to trash.

The agent reads these values at runtime.  Values such as confidence thresholds and
caps help mitigate accidental deletions.  The `SHADOW_MODE` flag allows you to run
the agent safely without actually moving any mail to Trash.
"""

from __future__ import annotations

from typing import List, Dict

# Timezone for timestamping logs and scheduling actions
TIMEZONE: str = "Europe/Athens"

# Labels used by the agent.  These will be created in Gmail if they do not exist.
LABELS: Dict[str, str] = {
    "TRASHED": "AI/Trash-After-Summary",
    "REVIEW": "AI/Review",
    "PROTECTED": "AI/Protected",
    "KEPT": "AI/Kept",
}

# Decision thresholds
THRESHOLDS: Dict[str, float | int] = {
    # Minimum classification confidence to auto‑trash a message
    "AUTO_TRASH_CONFIDENCE": 0.92,
    # Maximum number of threads to trash per run
    "MAX_TRASH_THREADS_PER_RUN": 10,
    # Maximum number of threads to trash per sender per run
    "MAX_TRASH_THREADS_PER_SENDER": 5,
}

# Run modes
MODES: Dict[str, bool] = {
    # When True, no messages are moved to Trash; the agent only labels and logs
    "SHADOW_MODE": True,
    # When True, the agent will move eligible threads to Trash
    "ENABLE_TRASH": False,
}

# Hard protection rules: domains and specific senders that should never be auto‑trashed
PROTECTED_DOMAINS: List[str] = [
    "tee.gr",
    "central.tee.gr",
    "formspree.io",
]

PROTECTED_SENDERS: List[str] = [
    "emkeepee-tee@central.tee.gr",
    "sign-noreply@tee.gr",
]

# Senders eligible for the trash lane.  Keep this list narrow until the agent is
# well tuned.  Only senders in this list will be auto‑trashed when confidence
# threshold conditions are met.
TRASH_LANE_SENDERS: List[str] = [
    "noreply@news.bloomberg.com",
    "bbg_nef@e.mail.bloomberg.net",
]

# Gmail search queries defining buckets of candidate threads.  Each entry has a
# name (used in logs) and a Gmail query string.  Only threads returned by
# these queries are processed by the classifier.
CANDIDATE_QUERIES: List[Dict[str, str]] = [
    {
        "name": "safe_newsletter_lane",
        "query": "in:inbox newer_than:7d -has:attachment (from:(news.bloomberg.com OR e.mail.bloomberg.net))",
    },
]

# Classification categories for reporting
CLASS_CATEGORIES: List[str] = [
    "newsletter",
    "market_briefing",
    "webinar_promo",
    "product_update",
    "human_conversation",
    "invoice_or_finance",
    "lead_or_customer",
    "compliance_or_admin",
    "personal",
    "unknown",
]

__all__ = [
    "TIMEZONE",
    "LABELS",
    "THRESHOLDS",
    "MODES",
    "PROTECTED_DOMAINS",
    "PROTECTED_SENDERS",
    "TRASH_LANE_SENDERS",
    "CANDIDATE_QUERIES",
    "CLASS_CATEGORIES",
]
