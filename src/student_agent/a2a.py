from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

Performative = Literal[
    "request",
    "inform",
    "propose",
    "reject",
    "confirm",
    "negotiate",
    "query",
]


@dataclass(frozen=True)
class A2AMessage:
    """Observable agent-to-agent envelope. Payload stays out of the public trace."""

    sender: str
    recipient: str
    performative: Performative
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    message_id: str = field(default_factory=lambda: f"a2a_{uuid4().hex[:16]}")
    in_reply_to: str | None = None
    parent_message_id: str | None = None
    hop_count: int = 0
    priority: str = "normal"
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )


class A2ABus:
    """In-process A2A bus with hop budget, peer-to-peer negotiation, and causality tracking."""

    def __init__(self, *, max_hops: int = 24) -> None:
        self.max_hops = max_hops
        self._log: list[A2AMessage] = []

    @property
    def log(self) -> tuple[A2AMessage, ...]:
        return tuple(self._log)

    def send(self, message: A2AMessage) -> A2AMessage:
        if len(self._log) >= self.max_hops:
            raise RuntimeError("A2A hop budget exhausted")
        self._log.append(message)
        return message

    def request(
        self,
        *,
        sender: str,
        recipient: str,
        intent: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
        parent_message_id: str | None = None,
    ) -> A2AMessage:
        return self.send(
            A2AMessage(
                sender=sender,
                recipient=recipient,
                performative="request",
                intent=intent,
                payload=payload or {},
                correlation_id=correlation_id,
                parent_message_id=parent_message_id,
                hop_count=len(self._log),
            )
        )

    def inform(
        self,
        *,
        sender: str,
        recipient: str,
        intent: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
        parent_message_id: str | None = None,
    ) -> A2AMessage:
        return self.send(
            A2AMessage(
                sender=sender,
                recipient=recipient,
                performative="inform",
                intent=intent,
                payload=payload or {},
                correlation_id=correlation_id,
                in_reply_to=in_reply_to,
                parent_message_id=parent_message_id,
                hop_count=len(self._log),
            )
        )

    def propose(
        self,
        *,
        sender: str,
        recipient: str,
        intent: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> A2AMessage:
        return self.send(
            A2AMessage(
                sender=sender,
                recipient=recipient,
                performative="propose",
                intent=intent,
                payload=payload or {},
                correlation_id=correlation_id,
                in_reply_to=in_reply_to,
                hop_count=len(self._log),
            )
        )

    def negotiate(
        self,
        *,
        sender: str,
        recipient: str,
        intent: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> A2AMessage:
        return self.send(
            A2AMessage(
                sender=sender,
                recipient=recipient,
                performative="negotiate",
                intent=intent,
                payload=payload or {},
                correlation_id=correlation_id,
                in_reply_to=in_reply_to,
                hop_count=len(self._log),
            )
        )

    def confirm(
        self,
        *,
        sender: str,
        recipient: str,
        intent: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> A2AMessage:
        return self.send(
            A2AMessage(
                sender=sender,
                recipient=recipient,
                performative="confirm",
                intent=intent,
                payload=payload or {},
                correlation_id=correlation_id,
                in_reply_to=in_reply_to,
                hop_count=len(self._log),
            )
        )

    def get_conversation(self, correlation_id: str) -> list[A2AMessage]:
        return [msg for msg in self._log if msg.correlation_id == correlation_id]

    def get_interactions_between(self, agent_a: str, agent_b: str) -> list[A2AMessage]:
        targets = {agent_a, agent_b}
        return [msg for msg in self._log if msg.sender in targets and msg.recipient in targets]

    def to_mermaid(self) -> str:
        """Export the message exchange into a Mermaid sequence diagram."""
        lines = ["sequenceDiagram", "    autonumber"]
        for msg in self._log:
            sender = msg.sender.replace("-", "_")
            recipient = msg.recipient.replace("-", "_")
            arrow = "->>" if msg.performative in ("request", "propose", "negotiate") else "-->>"
            desc = f"{msg.performative}: {msg.intent}"
            lines.append(f"    {sender}{arrow}{recipient}: {desc}")
        return "\n".join(lines)
