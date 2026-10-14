"""Keeping a long chat affordable: condensing its earlier turns.

Every round of a chat re-sends the whole conversation. A thread that had grown
over an afternoon of building dashboards re-read 850K tokens across one turn's
rounds, and the history -- not the question -- was most of every request.

Once the history passes a size threshold, the turns before the most recent few
are condensed into a written summary, and the model is sent that summary plus
the recent turns verbatim. What is STORED does not change: the user still sees
every message, and a later summary is built from the stored turns, never from a
summary of a summary alone.

The summary is written by the small model, and the call is recorded in the
audit like any other. If it fails, nothing changes and the full history is sent
as before -- condensing is a saving, never a requirement for a turn to work.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.conversation import Conversation
from app.core.config import Settings

log = logging.getLogger(__name__)

#: Rough characters per token for sizing the history. Deliberately low (more
#: tokens per character than prose), so JSON-heavy tool traffic trips the
#: threshold early rather than late.
_CHARS_PER_TOKEN = 3

#: The most transcript handed to the summariser, newest kept. Well inside the
#: small model's 200K-token window, whatever an old thread has accumulated.
_MAX_TRANSCRIPT_CHARS = 360_000

_TOOL_INPUT_CHARS = 500
_TOOL_RESULT_CHARS = 1_500

#: Shown while the summary is written; the chat renders it as progress.
CONDENSING_NOTE = "Condensing the earlier part of this conversation to keep it quick…"

_SYSTEM = """You condense the earlier part of a conversation between a user and a data assistant that builds dashboards and answers questions from a data warehouse. The assistant will continue the conversation from your summary plus the most recent messages, without the full transcript.

Write a factual summary as short bullet points grouped under headings, at most about 700 words. Preserve EXACTLY, copied character for character:
- every dashboard report_id, plan_id and version number, with the dashboard's title
- database, table, column and panel names that were discussed
- parameter values, filters and date ranges the user chose
- figures the user was given or asked about, with their units
- decisions and preferences the user stated (chart choices, what to include or leave out, naming)
- what was saved, edited or reverted, and what is still pending: a plan not yet saved, an edit previewed but not confirmed, a question the user has not answered
- errors or problems that are still unresolved

Do not invent, infer or round anything that is not in the transcript. Leave out SQL unless the user discussed a specific query. Leave out small talk. If an earlier summary is included, carry its facts forward unless the later transcript changed them."""


def _turn_starts(conversation: Conversation) -> list[int]:
    """Indices of messages where the user said something (not tool results)."""
    return [
        i for i, m in enumerate(conversation.messages)
        if m.role == "user" and any(
            isinstance(b, dict) and b.get("type") == "text" for b in m.content
        )
    ]


def estimated_tokens(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, default=str)) // _CHARS_PER_TOKEN


def summary_cut(conversation: Conversation, settings: Settings) -> int | None:
    """Where a new summary should end, or None when the chat is fine as it is.

    The cut is always the start of a user turn, so a tool call and its result
    are never separated, and the last CHAT_SUMMARY_KEEP_TURNS turns -- the
    current one included -- always reach the model verbatim.

    A summary is only written when it would absorb at least a trigger's worth
    of turns not already summarised. Without that, a chat building dashboards
    condensed on nearly every turn: its kept turns alone are larger than the
    trigger, so each summary left the chat still over it, and the next turn
    began another. Every summary re-writes the kept history to the prompt
    cache at the write price instead of reading it at a tenth, re-summarises
    the previous summary, and runs a model call before the turn can start.
    Waiting for a full trigger's worth spaces those out to roughly one per
    trigger's growth, which is about where the cache re-write stops costing
    more than the history it removes.
    """
    trigger = settings.CHAT_SUMMARY_TRIGGER_TOKENS
    keep = max(1, settings.CHAT_SUMMARY_KEEP_TURNS)
    if trigger <= 0:
        return None
    if estimated_tokens(conversation.to_model_messages()) < trigger:
        return None
    starts = _turn_starts(conversation)
    if len(starts) <= keep:
        return None
    cut = starts[-keep]
    if cut <= conversation.summary_through:
        return None
    start = conversation.summary_through if conversation.summary else 0
    unsummarised = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages[start:cut]
    ]
    if estimated_tokens(unsummarised) < trigger:
        return None
    return cut


def _render(block: dict[str, Any]) -> str | None:
    kind = block.get("type")
    if kind == "text":
        return block.get("text") or None
    if kind == "tool_use":
        args = json.dumps(block.get("input") or {}, default=str)[:_TOOL_INPUT_CHARS]
        return f"[tool call: {block.get('name')} {args}]"
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        text = str(content or "")[:_TOOL_RESULT_CHARS]
        return f"[tool result{' (error)' if block.get('is_error') else ''}: {text}]"
    return None  # thinking and anything else: not facts the summary needs


def _transcript(conversation: Conversation, start: int, cut: int) -> str:
    lines = []
    for message in conversation.messages[start:cut]:
        parts = [p for p in (_render(b) for b in message.content if isinstance(b, dict)) if p]
        if parts:
            lines.append(f"{message.role.upper()}: " + "\n".join(parts))
    text = "\n\n".join(lines)
    # An old thread can hold more than the summariser can read; its newest
    # part is what the rest of the conversation builds on.
    return text[-_MAX_TRANSCRIPT_CHARS:]


def condense(conversation: Conversation, settings: Settings, cut: int) -> bool:
    """Write the summary up to `cut` onto the conversation. Blocking (a model
    call); never raises. Returns whether the conversation changed."""
    from app.engine import llm

    transcript = _transcript(conversation, conversation.summary_through, cut)
    if not transcript.strip():
        return False
    earlier = (f"EARLIER SUMMARY (already condensed):\n{conversation.summary}\n\n"
               if conversation.summary else "")
    try:
        resp = llm._create(
            "chat_summary",
            model=llm.MODEL_SUMMARY,
            max_tokens=2000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": (
                f"{earlier}TRANSCRIPT TO CONDENSE:\n{transcript}\n\n"
                "Write the summary now."
            )}],
        )
        summary = llm._text(resp).strip()
    except Exception:  # noqa: BLE001 - the turn goes ahead with the full history
        log.warning("could not condense conversation %s; sending it whole",
                    conversation.conversation_id, exc_info=True)
        return False
    if not summary or getattr(resp, "stop_reason", None) == "max_tokens":
        # A summary cut off mid-way may have lost the facts at its end.
        log.warning("summary for %s was empty or cut off; sending the history whole",
                    conversation.conversation_id)
        return False
    conversation.summary = summary
    conversation.summary_through = cut
    return True
