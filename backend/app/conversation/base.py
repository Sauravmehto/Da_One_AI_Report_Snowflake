"""Conversation models + store protocol.

Mirrors app/session/ deliberately: a Protocol with an in-memory and a Redis
implementation, chosen by settings. Local dev runs in-memory; Redis becomes a
drop-in with no code change.

Two design choices worth knowing:

**Messages are stored as Anthropic content blocks, verbatim.** A tool-use
transcript is not plain text — it is text/tool_use/tool_result blocks, and the
agent loop has to hand them back to the model in exactly that shape on the
next turn. Storing anything else would mean lossy translation in both
directions.

**Conversations are owned by a USER, not a session.** Sessions expire (24h by
default) and are recreated on every login; chat history outliving that is the
whole point. Keying on the Snowflake identity means logging out and back in
keeps your history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


#: What the model is told about a tool call whose result was never recorded.
INTERRUPTED_TOOL_RESULT = (
    "This tool call was interrupted before it returned (the connection dropped or "
    "the turn was stopped), so its result is unknown. Do not assume it succeeded: "
    "check the current state before relying on it, and tell the user if it matters."
)


def repair_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The message list with every tool call answered and every answer asked for.

    The Messages API refuses a history in which a tool_use is not answered by a
    tool_result in the very next message, or a tool_result answers nothing. A
    turn cut off mid-tool (the tab closed, Stop pressed, a proxy dropping a quiet
    stream) stored the call without its result, and from then on EVERY message
    in that conversation failed with the same 400. Nothing the user could type
    would recover it.

    Repaired here, on the way to the model, rather than in storage: it heals
    conversations already saved in that state, and whatever else leaves one
    behind later. Stored history is not rewritten. The output is a pure function
    of the input, so a repaired prefix is identical turn to turn and prompt
    caching is unaffected.
    """
    copies = [{"role": m["role"], "content": list(m["content"])} for m in messages]

    # Pass 1: every call gets an answer in the message straight after it.
    answered: list[dict[str, Any]] = []
    for position, message in enumerate(copies):
        answered.append(message)
        if message["role"] != "assistant":
            continue
        asked = _ordered_tool_use_ids(message["content"])
        if not asked:
            continue
        following = copies[position + 1] if position + 1 < len(copies) else None
        if following is not None and following["role"] == "user":
            have = {b.get("tool_use_id") for b in following["content"] if _is_result(b)}
            missing = [tool_id for tool_id in asked if tool_id not in have]
            if missing:
                # Results first, then the user's own words: that is where the
                # API looks for them, and nothing the user wrote is lost.
                results = [b for b in following["content"] if _is_result(b)]
                others = [b for b in following["content"] if not _is_result(b)]
                following["content"] = results + _stand_ins(missing) + others
        else:
            answered.append({"role": "user", "content": _stand_ins(asked)})

    # Pass 2: drop answers to calls that are not directly before them.
    out: list[dict[str, Any]] = []
    for message in answered:
        if message["role"] == "user":
            previous = out[-1] if out else None
            asked_ids = (set(_ordered_tool_use_ids(previous["content"]))
                         if previous and previous["role"] == "assistant" else set())
            content = [b for b in message["content"]
                       if not (_is_result(b) and b.get("tool_use_id") not in asked_ids)]
            if not content:
                continue
            message = {**message, "content": content}
        out.append(message)
    return out


def _stand_ins(tool_ids: list[str]) -> list[dict[str, Any]]:
    return [{"type": "tool_result", "tool_use_id": tool_id,
             "content": INTERRUPTED_TOOL_RESULT, "is_error": True}
            for tool_id in tool_ids]


def _is_result(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_result"


def _ordered_tool_use_ids(content: list[Any]) -> list[str]:
    return [block["id"] for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")]


#: Introduces a conversation's summary to the model.
SUMMARY_PREFIX = (
    "[Summary of the earlier part of this conversation, condensed automatically "
    "to save context. The user can still see every earlier message. If you need "
    "an exact value, SQL or result from earlier that is not in this summary, run "
    "the tool again rather than guessing.]\n\n"
)


ConversationKind = Literal["chat", "dashboard_edit"]


class StoredMessage(BaseModel):
    """One turn, in the Anthropic Messages shape.

    `content` holds raw content blocks (text / tool_use / tool_result) so it
    round-trips into the model without translation.
    """

    role: Literal["user", "assistant"]
    content: list[dict[str, Any]]
    created_at: datetime = Field(default_factory=_now)


class Conversation(BaseModel):
    conversation_id: str
    owner: str                      # Snowflake user — see module docstring
    database: str | None = None     # locked on first message, as the UI does
    # Set when this thread belongs to a dashboard's side panel rather than the
    # main chat. Scopes the conversation to that dashboard: it is reopened when
    # you return to the dashboard, and deliberately kept OUT of the sidebar's
    # chat list, where a few questions per dashboard would bury real threads.
    dashboard_id: str | None = None
    # What drives the thread. "chat" is the agent loop (main chat and a
    # dashboard's Ask panel); "dashboard_edit" is the assistant inside a
    # dashboard's Edit panel, whose turns are plain text and which the agent
    # loop must never resume. Absent on every thread saved before it existed,
    # which reads as "chat" — what they all were.
    kind: ConversationKind = "chat"
    title: str | None = None
    messages: list[StoredMessage] = Field(default_factory=list)
    # Rendered artifacts (charts) keyed by the tool_use id that produced them.
    # Deliberately NOT in `messages`: the model is handed a summary of a chart
    # rather than its rows, and that must stay true on replay. Keeping the full
    # payload in a sibling field lets the UI redraw the chart on reload without
    # ever feeding those rows back into the context.
    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pinned: bool = False
    # A written summary of messages[:summary_through], sent to the model in
    # their place once a chat grows long (app/agent/history.py). The messages
    # themselves are never removed: the user still sees all of them. A version
    # that predates these fields ignores them and sends the history whole.
    summary: str | None = None
    summary_through: int = 0
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def append(self, role: Literal["user", "assistant"],
               content: list[dict[str, Any]]) -> None:
        self.messages.append(StoredMessage(role=role, content=content))
        self.updated_at = _now()

    def record_artifact(self, tool_use_id: str, artifact: dict[str, Any]) -> None:
        """Keep a rendered artifact so a resumed conversation still shows it."""
        self.artifacts[tool_use_id] = artifact
        self.updated_at = _now()

    def to_model_messages(self) -> list[dict[str, Any]]:
        """The message list to send to the model, with any broken tool call
        repaired (see repair_tool_pairs).

        With a summary, the messages it covers are replaced by it: the summary
        leads the first message after them, which always starts a user turn.
        The result depends only on stored state, so it is identical from turn to
        turn until the next summary is written, and the prompt cache holds.
        """
        start = self.summary_through if self.summary else 0
        start = min(max(start, 0), len(self.messages))
        visible = [{"role": m.role, "content": m.content} for m in self.messages[start:]]
        if self.summary:
            lead = {"type": "text", "text": SUMMARY_PREFIX + self.summary}
            if visible and visible[0]["role"] == "user":
                visible[0] = {"role": "user", "content": [lead, *visible[0]["content"]]}
            else:
                visible.insert(0, {"role": "user", "content": [lead]})
        return repair_tool_pairs(visible)


class ConversationSummary(BaseModel):
    """List-view metadata — never carries message bodies."""

    conversation_id: str
    title: str | None
    database: str | None
    dashboard_id: str | None = None
    kind: ConversationKind = "chat"
    pinned: bool = False
    message_count: int
    created_at: datetime
    updated_at: datetime


class ConversationStore(Protocol):
    async def get(self, conversation_id: str) -> Conversation | None: ...

    async def save(self, conversation: Conversation, ttl_seconds: int) -> None: ...

    async def list_for_owner(self, owner: str) -> list[ConversationSummary]: ...

    async def delete(self, conversation_id: str) -> None: ...

    async def close(self) -> None: ...
