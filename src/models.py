from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


ALLOWED_DECISIONS = {"keep", "review", "summarize_then_trash"}


@dataclass
class MessageContext:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    body_text: str
    has_attachments: bool
    is_reply_thread: bool
    labels: list[str] = field(default_factory=list)


@dataclass
class ClassificationResult:
    decision: str
    confidence: float
    reason: str
    summary: str
    protection_hits: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.decision not in ALLOWED_DECISIONS:
            raise ValueError(f"Invalid decision: {self.decision}")


@dataclass
class AuditRecord:
    timestamp: str
    sender: str
    subject: str
    decision: str
    confidence: float
    reason: str
    summary: str
    action_taken: str
    protection_hits: list[str]
    thread_id: str
    message_id: str
    error: str = ""

    @classmethod
    def create(
        cls,
        context: MessageContext,
        result: ClassificationResult,
        action_taken: str,
        error: str = "",
    ) -> "AuditRecord":
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            sender=context.sender,
            subject=context.subject,
            decision=result.decision,
            confidence=result.confidence,
            reason=result.reason,
            summary=result.summary,
            action_taken=action_taken,
            protection_hits=result.protection_hits,
            thread_id=context.thread_id,
            message_id=context.message_id,
            error=error,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
