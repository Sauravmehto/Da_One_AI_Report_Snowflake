"""The streaming agent loop.

Replaces the one-shot classifier: the model chooses tools, sees their results,
and keeps going until it has an answer — the job Claude Desktop did in the MCP
deployment.

Shape of a turn:

    user message
      -> model streams prose and/or asks for tools
      -> tools run (off the event loop; the engine is blocking)
      -> results go back as a user turn
      -> repeat until the model stops asking for tools

Everything is emitted as events so the UI can show progress rather than a dead
spinner: text deltas as they generate, and a start/end pair per tool call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from app.agent import history
from app.agent.prompts import build_system_prompt
from app.agent.tools import TOOL_DEFINITIONS, ToolContext, dispatch
from app.conversation import Conversation, truncate_tool_result
from app.conversation.base import INTERRUPTED_TOOL_RESULT
from app.core.config import Settings
from app.engine import audit

log = logging.getLogger(__name__)


@dataclass
class Event:
    """One SSE frame. `type` is the event name, `data` its JSON payload."""

    type: str
    data: dict[str, Any]


@dataclass
class _TurnUsage:
    """What one user turn cost, accumulated across its tool rounds.

    A turn is one row in the audit log, not one row per model round: twelve
    single-row Snowflake INSERTs per question is real latency, and the per-round
    split is not a question anyone asks. `calls` keeps the part of that detail
    that matters — how many times the transcript had to be re-sent.

    `input_tokens` is the API's own figure and is the UNCACHED REMAINDER, not
    the prompt size: with the breakpoint at the end of the transcript it falls
    to single digits, and the volume shows up in the two cache fields instead.
    Anything reporting on these has to sum all three — see sql/audit_token_usage.sql.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    status: str = "ok"
    error: str | None = None

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        self.cache_write_tokens += int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )


#: The chat's cache lifetime: an hour, not the default five minutes. Building a
#: dashboard runs the planner INSIDE a turn, and a pause to read a plan or a
#: chart is often longer than five minutes too; either way the five-minute
#: entry had expired and the whole conversation was re-sent at full price. A
#: one-hour write costs 2x input rather than 1.25x, and pays for itself on the
#: first long gap. Every chat breakpoint must use the same lifetime: the API
#: rejects a one-hour entry placed after a five-minute one.
CHAT_CACHE: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}


def _with_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """`messages` with a cache breakpoint on its final content block.

    Without this every round re-reads the whole transcript at full price. The
    breakpoint moves to the end on each request, so each one reads everything
    the previous one wrote and pays only for what it added.

    Copy-on-write, and that is the whole point of the function: `content` is the
    SAME list the Conversation holds, so setting cache_control on the block in
    place would persist it, replay it on the next turn, and accumulate one dead
    breakpoint per turn until the request exceeds the four the API allows.

    Placed every round rather than once per turn on purpose. A breakpoint
    searches back at most 20 positions for a prior entry; a turn that runs the
    tool loop to its ceiling adds more than that, so a once-per-turn marker
    would quietly stop finding anything on exactly the long turns worth caching.
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if not isinstance(content, list) or not content:
        return messages
    block = content[-1]
    if not isinstance(block, dict):
        return messages

    marked = {**block, "cache_control": CHAT_CACHE}
    return [*messages[:-1], {**last, "content": [*content[:-1], marked]}]


def _blocks_to_dicts(content: Any) -> list[dict[str, Any]]:
    """SDK content blocks -> plain dicts for storage and replay.

    Stored verbatim: the next turn hands these straight back to the model, so
    any lossy conversion here breaks tool continuity.
    """
    out: list[dict[str, Any]] = []
    for block in content:
        if hasattr(block, "model_dump"):
            out.append(block.model_dump(exclude_none=True))
        elif isinstance(block, dict):
            out.append(block)
    return out


def _summarise_for_ui(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """A compact description of what a tool did, for the activity line.

    Deliberately not the payload: the UI gets shape and outcome, and renders
    the real data from the dedicated dashboard/table components.
    """
    if "error" in result:
        return {"ok": False, "detail": str(result["error"])[:300]}

    detail: str | None = None
    # `rendered` marks the model-facing summary a chart result is swapped for
    # once the artifact has been sent to the UI.
    if name == "show_chart" and result.get("rendered"):
        detail = f"{result.get('row_count', 0)} rows charted"
    elif "row_count" in result:
        detail = f"{result['row_count']} rows"
    elif "panels" in result and isinstance(result["panels"], list):
        detail = f"{len(result['panels'])} panels"
    elif "dashboards" in result and isinstance(result["dashboards"], list):
        detail = f"{len(result['dashboards'])} dashboards"
    elif name == "explain_concept":
        detail = "found" if result.get("found") else "not in glossary"
    elif "plan" in result and isinstance(result.get("plan"), dict):
        panels = result["plan"].get("panels") or []
        detail = f"planned {len(panels)} panels"

    return {"ok": True, "detail": detail}


async def run_turn(
    *,
    client: AsyncAnthropic,
    settings: Settings,
    conversation: Conversation,
    user_message: str,
    ctx: ToolContext,
    system: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> AsyncIterator[Event]:
    """Run one user turn to completion, yielding events as it goes.

    `conversation` is mutated in place — the caller persists it afterwards, so
    a turn that fails midway still saves what actually happened rather than
    losing the exchange.

    `system` and `tools` default to the main chat's. A caller passes its own to
    scope a turn differently — the dashboard side panel supplies a prompt built
    around one saved dashboard and a tool list with the mutating tools removed.

    The turn's token usage is written to the audit log once the events are
    exhausted. A client that disconnects mid-answer loses that row: recording
    it would mean awaiting inside a closing generator, and a missing audit row
    is a smaller problem than a turn that cannot be shut down cleanly.
    """
    tally = _TurnUsage()
    started = time.monotonic()

    async for event in _run_turn(
        client=client,
        settings=settings,
        conversation=conversation,
        user_message=user_message,
        ctx=ctx,
        system=system,
        tools=tools,
        tally=tally,
    ):
        yield event

    await _record_turn(
        settings=settings,
        conversation=conversation,
        user_message=user_message,
        ctx=ctx,
        tally=tally,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def _record_turn(
    *,
    settings: Settings,
    conversation: Conversation,
    user_message: str,
    ctx: ToolContext,
    tally: _TurnUsage,
    duration_ms: int,
) -> None:
    """Write one audit row for the turn. Off the event loop; never raises.

    log_event is a synchronous Snowflake INSERT. Tools reach it through
    asyncio.to_thread already; this path is the async loop itself, where
    calling it directly would stall every other request on the worker for the
    length of the round trip.
    """
    if tally.calls == 0:
        return
    await asyncio.to_thread(
        audit.log_event,
        "agent_turn",
        tally.status,
        ctx.snowflake_user,
        ctx.role,
        question=user_message,
        error=tally.error,
        duration_ms=duration_ms,
        model=settings.ANTHROPIC_MODEL_AGENT,
        input_tokens=tally.input_tokens,
        output_tokens=tally.output_tokens,
        cache_read_tokens=tally.cache_read_tokens,
        cache_write_tokens=tally.cache_write_tokens,
        conversation_id=conversation.conversation_id,
        model_calls=tally.calls,
    )


#: Tools that write their own audit row for every outcome, failures included.
_SELF_AUDITED_TOOLS = frozenset({"run_safe_sql", "show_chart", "ask_data_question"})


async def _record_tool_error(name: str, result: dict[str, Any], user_message: str,
                             ctx: ToolContext, conversation: Conversation) -> None:
    """An audit row for a tool that failed and does not record itself.

    A failed edit reached the model as {"error": ...} and the turn still logged
    "ok", so six failed dashboard edits were invisible in the audit. Failures
    only: a successful save or edit is already in the dashboard's version
    history. Never raises.
    """
    if name in _SELF_AUDITED_TOOLS:
        return
    await asyncio.to_thread(
        audit.log_event,
        name,
        "error",
        ctx.snowflake_user,
        ctx.role,
        question=user_message,
        error=str(result.get("error"))[:2000],
        client_info="webapp",
        conversation_id=conversation.conversation_id,
    )


async def _record_plan_outcome(name: str, result: dict[str, Any], user_message: str,
                               ctx: ToolContext, conversation: Conversation) -> None:
    """An audit row for every plan, saying how many of its panels survived.

    A plan that drops panels whose SQL fails its dry-run is a success, so it
    logged nothing — the dropped panels went to the assistant and nowhere else.
    Whether to spend a repair call on failing panels has to be decided from how
    often they fail, and that was not recorded.

    ROW_COUNT is the panels kept. STATUS is "partial" when any were dropped,
    with the count and each dropped panel's reason in ERROR_MESSAGE, so
    COUNT_IF(STATUS = 'partial') over plan rows is the drop rate. Never raises.
    """
    skipped = result.get("skipped_panels") or []
    kept = result.get("panel_count")
    total = (kept or 0) + len(skipped)
    detail = None
    if skipped:
        reasons = "; ".join(
            f"'{s.get('title')}': {s.get('reason')}" for s in skipped if isinstance(s, dict)
        )
        detail = f"{len(skipped)} of {total} panels dropped: {reasons}"[:2000]
    await asyncio.to_thread(
        audit.log_event,
        name,
        "partial" if skipped else "ok",
        ctx.snowflake_user,
        ctx.role,
        question=user_message,
        row_count=kept,
        error=detail,
        client_info="webapp",
        conversation_id=conversation.conversation_id,
    )


_CUT_OFF_TOOL_RESULT = (
    "This tool call was never run: the reply was cut off by its length limit before "
    "the call was complete. Nothing was changed."
)


def _answer_unfinished(conversation: Conversation, content: list[Any],
                       tool_results: list[dict[str, Any]], reason: str) -> None:
    """Store this turn's tool results, answering any call that has none.

    The API requires every tool_use to be answered in the next message. Called
    on each way out of a tool round, so a call can never be stored alone.
    """
    answered = {result["tool_use_id"] for result in tool_results}
    for block in content:
        if getattr(block, "type", None) == "tool_use" and block.id not in answered:
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": reason, "is_error": True})
    if tool_results:
        conversation.append("user", tool_results)


async def _run_turn(
    *,
    client: AsyncAnthropic,
    settings: Settings,
    conversation: Conversation,
    user_message: str,
    ctx: ToolContext,
    system: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    tally: _TurnUsage,
) -> AsyncIterator[Event]:
    """The turn itself. Split from run_turn only so the audit row can be
    written once, after whichever of the several exits the turn takes."""
    conversation.append("user", [{"type": "text", "text": user_message}])
    if system is None:
        system = build_system_prompt(ctx.database, ctx.semantic_context)
    if tools is None:
        tools = TOOL_DEFINITIONS

    yield Event("message_start", {"conversation_id": conversation.conversation_id})

    # A long history is condensed before this turn's first request, never
    # between rounds: the rounds of one turn then share one stable prefix.
    cut = history.summary_cut(conversation, settings)
    if cut is not None:
        yield Event("thinking", {"delta": history.CONDENSING_NOTE})
        with audit.conversation(conversation.conversation_id):
            await asyncio.to_thread(history.condense, conversation, settings, cut)

    for iteration in range(settings.AGENT_MAX_ITERATIONS):
        try:
            async with client.messages.stream(
                model=settings.ANTHROPIC_MODEL_AGENT,
                max_tokens=settings.AGENT_MAX_TOKENS,
                system=system,
                tools=tools,
                messages=_with_cache_breakpoint(
                    conversation.to_model_messages()
                ),
                # Asked for explicitly rather than inherited. The model runs
                # adaptive thinking whether or not this is set, but its default
                # display is "omitted" — thinking blocks arrive with empty text,
                # so a turn spent reasoning streamed NOTHING and read as a hang.
                # "summarized" gives us something to show while it works.
                thinking={"type": "adaptive", "display": "summarized"},
            ) as stream:
                # The raw event stream, not text_stream: that helper yields only
                # text deltas, and the whole point here is to also surface the
                # thinking the user was previously left staring at nothing for.
                async for event in stream:
                    if event.type == "text":
                        yield Event("text", {"delta": event.text})
                    elif event.type == "thinking":
                        yield Event("thinking", {"delta": event.thinking})
                final = await stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            log.exception("agent stream failed")
            tally.status = "error"
            tally.error = str(exc)[:2000]
            yield Event("error", {"message": f"Model request failed: {exc}"})
            return

        conversation.append("assistant", _blocks_to_dicts(final.content))

        usage = getattr(final, "usage", None)
        tally.add(usage)
        log.info(
            "agent turn %d: stop_reason=%s in=%s out=%s cache_read=%s cache_write=%s",
            iteration + 1,
            final.stop_reason,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(usage, "cache_read_input_tokens", None),
            getattr(usage, "cache_creation_input_tokens", None),
        )

        if final.stop_reason == "max_tokens":
            # A reply cut off can end in a half-written tool call. Answer it here,
            # or the stored history is invalid and the next message fails.
            _answer_unfinished(conversation, final.content, [], _CUT_OFF_TOOL_RESULT)
            # The silent ending. The turn stopped because it ran out of room,
            # which is not an answer and must not look like one — say so rather
            # than leaving a half-written reply on screen with no explanation.
            log.warning(
                "agent turn hit max_tokens (%s); the reply is truncated",
                settings.AGENT_MAX_TOKENS,
            )
            tally.status = "truncated"
            tally.error = f"hit AGENT_MAX_TOKENS ({settings.AGENT_MAX_TOKENS})"
            yield Event(
                "error",
                {
                    "message": (
                        "The reply was cut off — this turn reached its token "
                        "limit before finishing. Ask again, more narrowly, or "
                        "raise AGENT_MAX_TOKENS."
                    )
                },
            )
            return

        if final.stop_reason != "tool_use":
            yield Event("message_stop", {"stop_reason": final.stop_reason})
            return

        # Run every tool the model asked for, then hand the results back as a
        # single user turn — the shape the Messages API expects.
        tool_results: list[dict[str, Any]] = []
        try:
            for block in final.content:
                if getattr(block, "type", None) != "tool_use":
                    continue

                tool_input = dict(block.input or {})
                yield Event("tool_start", {
                    "id": block.id,
                    "name": block.name,
                    "input": tool_input,
                })

                # Rows written inside the tool -- its SQL, the planner's model
                # calls -- carry this conversation. No yield inside the block:
                # the tag must not outlive this call.
                with audit.conversation(conversation.conversation_id):
                    result = await dispatch(block.name, tool_input, ctx)
                if isinstance(result, dict) and "error" in result:
                    await _record_tool_error(block.name, result, user_message, ctx,
                                             conversation)
                elif isinstance(result, dict) and isinstance(result.get("skipped_panels"), list):
                    await _record_plan_outcome(block.name, result, user_message, ctx,
                                               conversation)

                # A renderable result goes to the UI in FULL, while the model gets
                # the same truncated summary as any other tool. The browser can
                # happily chart 100 rows; spending the model's context on them
                # would fill it within a few charts for no benefit — it needs to
                # know what was plotted, not every value.
                panel = result.get("panel") if isinstance(result, dict) else None
                if panel:
                    yield Event("artifact", {
                        "id": block.id,
                        "kind": "chart",
                        "panel": panel,
                    })
                    # Store it too, or the chart exists only in the browser tab that
                    # happened to be open when it streamed — reopening the
                    # conversation would show the commentary with nothing above it.
                    conversation.record_artifact(
                        block.id, {"kind": "chart", "panel": panel}
                    )
                    # The chart is already on screen. Hand the model a description
                    # of what it plotted rather than the rows themselves — it needs
                    # to comment on the chart, not re-read the data.
                    result = {
                        "rendered": True,
                        "title": panel.get("title"),
                        "chart_type": panel.get("chart_type"),
                        "x": panel.get("x"),
                        "y": panel.get("y"),
                        "row_count": panel.get("row_count"),
                        "columns": panel.get("columns"),
                        "sql": result.get("sql"),
                        "note": "Chart is displayed to the user. Do not restate every value.",
                    }

                content, truncated = truncate_tool_result(result)

                yield Event("tool_end", {
                    "id": block.id,
                    "name": block.name,
                    "truncated": truncated,
                    **_summarise_for_ui(block.name, result),
                })

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    **({"is_error": True} if "error" in result else {}),
                })

        finally:
            # Recorded on every exit, including a generator torn down mid-tool.
            # A client that disconnects cancels the turn at an await, and
            # cancellation is not an Exception, so dispatch's own handler never
            # sees it. The route's finally then saved the call with no result:
            # a transcript the API refuses for every message after it.
            _answer_unfinished(conversation, final.content, tool_results,
                               INTERRUPTED_TOOL_RESULT)

    # Iteration ceiling hit. Say so plainly rather than truncating in silence —
    # the user needs to know the answer is incomplete.
    tally.status = "error"
    tally.error = (
        f"stopped after {settings.AGENT_MAX_ITERATIONS} tool rounds"
    )
    yield Event("error", {
        "message": (
            f"Stopped after {settings.AGENT_MAX_ITERATIONS} tool rounds without "
            "reaching an answer. Try narrowing the question."
        )
    })


def format_sse(event: Event) -> str:
    """Encode an Event as an SSE frame."""
    return f"event: {event.type}\ndata: {json.dumps(event.data, default=str)}\n\n"
