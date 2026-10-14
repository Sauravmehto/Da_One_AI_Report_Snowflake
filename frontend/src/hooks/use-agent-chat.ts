import * as React from "react"

import type { ChatMessage } from "@/components/chat/chat-messages"
import {
  appendTextBlock,
  pushChartBlock,
  pushToolBlock,
  updateToolBlock,
  withSyncedFields,
} from "@/components/chat/chat-messages"
import type { ToolActivity } from "@/components/chat/tool-activity"
import { streamAgentChat, type AgentRequestBody } from "@/lib/agent-stream"

/**
 * One turn of the streaming agent, as UI state.
 *
 * Extracted from ChatView so the dashboard side panel runs the same loop
 * rather than a second copy of it. The event handling here is fiddly in ways
 * that were arrived at by fixing real bugs — patching only the in-flight
 * message, folding tool results into the preceding turn, keeping chart
 * artifacts out of the model's context — and two implementations of that would
 * drift apart the first time either was touched.
 *
 * What is deliberately NOT in here: anything about the sidebar, the active
 * conversation, or the database picker. The main chat owns those globals; the
 * dashboard panel must not touch them, or asking a question about a panel
 * would hijack which thread the sidebar thinks you are in. Callers hook in
 * through `onConversationStarted` / `onTurnComplete` instead.
 */

function newId(): string {
  return Math.random().toString(36).slice(2)
}

function formatApiError(err: unknown): string {
  if (!(err instanceof Error)) {
    return "Something went wrong. Please try again."
  }
  try {
    const parsed = JSON.parse(err.message) as { detail?: string }
    if (typeof parsed.detail === "string") {
      return parsed.detail
    }
  } catch {
    // Not JSON — use the raw message below.
  }
  return err.message || "Something went wrong. Please try again."
}

type TextSmoother = {
  push: (text: string) => void
  end: () => void
  flush: () => void
}

/**
 * Paces text onto the screen so it reads as writing rather than as arriving.
 *
 * The model's deltas are not smooth: `stream.text_stream` hands over whatever
 * the API sent, frequently a whole sentence, and each one becomes a state
 * update that React may batch with the next. The result lurches — a paragraph
 * appears at once, then nothing, then another.
 *
 * So the deltas go into a queue and a rAF loop drains it. The drain is
 * PROPORTIONAL (a fixed share of whatever is waiting, with a floor) rather
 * than a fixed characters-per-frame: a fast answer with a large backlog
 * catches up quickly, a slow trickle still moves visibly, and neither can fall
 * behind the stream and finish long after the model has stopped.
 */
function createTextSmoother(
  append: (chunk: string) => void,
  onDrained: () => void
): TextSmoother {
  let queue = ""
  let frame: number | null = null
  let ended = false
  let settled = false

  const hidden = () =>
    typeof document !== "undefined" && document.visibilityState === "hidden"

  /** Stop pacing and show everything immediately. */
  function finish() {
    if (frame !== null) {
      cancelAnimationFrame(frame)
      frame = null
    }
    if (queue) {
      append(queue)
      queue = ""
    }
    settle()
  }

  /** Resolve the turn exactly once, and stop listening. */
  function settle() {
    if (!ended || settled) return
    settled = true
    document.removeEventListener("visibilitychange", onVisibilityChange)
    onDrained()
  }

  // requestAnimationFrame does not run in a background tab. Pacing there would
  // freeze the answer mid-sentence and — because the turn waits for the queue
  // to drain — leave the composer disabled until the person came back. Worse,
  // a frame already scheduled when the tab hides never fires at all, so this
  // has to be an event rather than a check inside the loop. Nobody is watching
  // an animation they cannot see: hidden means show it all.
  function onVisibilityChange() {
    if (hidden()) finish()
  }
  document.addEventListener("visibilitychange", onVisibilityChange)

  const step = () => {
    const take = Math.max(2, Math.ceil(queue.length / 6))
    append(queue.slice(0, take))
    queue = queue.slice(take)

    if (queue.length === 0) {
      frame = null
      settle()
      return
    }
    frame = requestAnimationFrame(step)
  }

  return {
    push(text: string) {
      if (settled) return
      queue += text
      if (hidden()) {
        finish()
        return
      }
      if (frame === null) frame = requestAnimationFrame(step)
    },
    /** No more text is coming; resolve once what is queued has been shown. */
    end() {
      ended = true
      if (frame === null) settle()
    },
    /** Abandon pacing and show everything now — for errors, where the message
     *  matters more than the animation. */
    flush() {
      ended = true
      finish()
    },
  }
}

/** Honour the OS setting: paced text is animation, and some people can't use it. */
function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  )
}

export type UseAgentChatOptions = {
  /**
   * Extra fields for the FIRST request of a thread — `{ database }` for the
   * main chat, `{ dashboard_id }` for a dashboard panel. Ignored once a
   * conversation exists, since the server fixes the scope at that point.
   */
  startWith: () => Partial<AgentRequestBody> | null
  onConversationStarted?: (conversationId: string, isNew: boolean) => void
  onTurnComplete?: () => void
}

export function useAgentChat({
  startWith,
  onConversationStarted,
  onTurnComplete,
}: UseAgentChatOptions) {
  const [messages, setMessages] = React.useState<ChatMessage[]>([])
  // The composer's text lives here rather than in each caller: it is part of
  // the chat session (clearing the thread clears the half-typed question), and
  // holding it outside meant every caller re-implemented that pairing.
  const [input, setInput] = React.useState("")
  const [isLoading, setIsLoading] = React.useState(false)
  const conversationIdRef = React.useRef<string | null>(null)
  const abortControllerRef = React.useRef<AbortController | null>(null)

  // Read through refs so `send` keeps a stable identity: it lands in effect
  // dependency lists, and a new function every render would re-fire them.
  const startWithRef = React.useRef(startWith)
  const startedRef = React.useRef(onConversationStarted)
  const completeRef = React.useRef(onTurnComplete)
  const inputRef = React.useRef(input)
  const loadingRef = React.useRef(isLoading)
  React.useEffect(() => {
    startWithRef.current = startWith
    startedRef.current = onConversationStarted
    completeRef.current = onTurnComplete
    inputRef.current = input
    loadingRef.current = isLoading
  })

  const send = React.useCallback(async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed) return

    const start = conversationIdRef.current ? {} : startWithRef.current()
    // A null start means the caller is not ready (no database chosen yet).
    if (start === null) return

    const assistantId = newId()
    setMessages((prev) => [
      ...prev,
      { id: newId(), role: "user", content: trimmed },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        blocks: [],
        streaming: true,
      },
    ])
    setIsLoading(true)
    const abortController = new AbortController()
    abortControllerRef.current = abortController

    // Patch only the in-flight assistant message; every earlier message is
    // left untouched so previous turns never re-render or get clobbered.
    const patch = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((prev) => prev.map((m) => (m.id === assistantId ? fn(m) : m)))

    // After a tool call, the next text belongs in a NEW block so the tool sits
    // between paragraphs chronologically (not above all text).
    let startNewTextBlock = false

    const appendText = (chunk: string) => {
      patch((m) => {
        let nextChunk = chunk
        if (startNewTextBlock && nextChunk) {
          nextChunk = nextChunk.replace(/^\n+/, "")
        }
        // Keep the "new block" flag until real text arrives, so an empty flush
        // after a tool does not accidentally continue the prior text block.
        if (!nextChunk) return m
        const forceNew = startNewTextBlock
        startNewTextBlock = false
        const blocks = appendTextBlock(
          m.blocks ?? [],
          nextChunk,
          newId,
          forceNew
        )
        return withSyncedFields(m, blocks)
      })
    }

    // The turn is not finished when the network is: text is still being paced
    // onto the screen. Resolving this is what lets the cursor and the composer
    // wait for the last word rather than snapping ahead of it.
    let resolveDrained: () => void = () => {}
    let drained = new Promise<void>((resolve) => {
      resolveDrained = resolve
    })
    const smoothed = !prefersReducedMotion()
    let smoother = createTextSmoother(appendText, () => resolveDrained())

    function resetSmoother() {
      // flush() settles the previous smoother; open a new drain gate for text
      // that arrives after this tool (or after an error path recreates it).
      resolveDrained()
      drained = new Promise<void>((resolve) => {
        resolveDrained = resolve
      })
      smoother = createTextSmoother(appendText, () => resolveDrained())
    }

    try {
      await streamAgentChat(
        {
          message: trimmed,
          ...(conversationIdRef.current
            ? { conversation_id: conversationIdRef.current }
            : start),
        },
        (event) => {
          if (abortController.signal.aborted) return

          switch (event.type) {
            case "message_start": {
              const isNew =
                conversationIdRef.current !== event.data.conversation_id
              conversationIdRef.current = event.data.conversation_id
              startedRef.current?.(event.data.conversation_id, isNew)
              break
            }

            case "text":
              // Reasoning is a progress cue for the wait, not part of the
              // answer — once the answer starts, it has done its job.
              patch((m) => (m.thinking ? { ...m, thinking: null } : m))
              if (smoothed) smoother.push(event.data.delta)
              else appendText(event.data.delta)
              break

            case "thinking":
              patch((m) => ({
                ...m,
                thinking: (m.thinking ?? "") + event.data.delta,
              }))
              break

            case "tool_start":
              // Drain paced text first so the tool lands under what was said.
              smoother.flush()
              resetSmoother()
              startNewTextBlock = true
              patch((m) => {
                const activity: ToolActivity = {
                  id: event.data.id,
                  name: event.data.name,
                  status: "running",
                  input: event.data.input,
                }
                return withSyncedFields(
                  m,
                  pushToolBlock(m.blocks ?? [], activity)
                )
              })
              break

            case "artifact":
              if (event.data.kind === "chart") {
                patch((m) =>
                  withSyncedFields(
                    m,
                    pushChartBlock(
                      m.blocks ?? [],
                      event.data.panel as unknown as NonNullable<
                        ChatMessage["charts"]
                      >[number],
                      newId
                    )
                  )
                )
              }
              break

            case "tool_end":
              patch((m) =>
                withSyncedFields(
                  m,
                  updateToolBlock(m.blocks ?? [], event.data.id, {
                    status: event.data.ok ? "ok" : "error",
                    detail: event.data.detail ?? null,
                  })
                )
              )
              break

            case "error":
              // Land any paced text first, or the error would be appended to
              // content the queue is still about to extend — and end up in the
              // middle of the sentence it interrupted.
              smoother.flush()
              resetSmoother()
              patch((m) => {
                const msg = event.data.message
                const blocks = appendTextBlock(
                  m.blocks ?? [],
                  m.content ? `\n\n${msg}` : msg,
                  newId,
                  true
                )
                return { ...withSyncedFields(m, blocks), isError: true }
              })
              break

            case "message_stop": {
              // end_turn is the normal, silent ending. Anything else stopped
              // the turn for a reason the reader cannot see — and a reply that
              // simply stops reads as the app hanging.
              const reason = event.data.stop_reason
              patch((m) => (m.thinking ? { ...m, thinking: null } : m))
              if (reason && reason !== "end_turn" && reason !== "stop_sequence") {
                smoother.flush()
                resetSmoother()
                patch((m) => {
                  const note = `The turn ended early (${reason}).`
                  const blocks = appendTextBlock(
                    m.blocks ?? [],
                    m.content ? `\n\n${note}` : note,
                    newId,
                    true
                  )
                  return { ...withSyncedFields(m, blocks), isError: true }
                })
              }
              break
            }
          }
        },
        abortController.signal
      )
      // Network done; wait for the last queued characters to be shown.
      smoother.end()
      await drained
    } catch (err) {
      // User clicked Stop — keep the partial answer and mark the turn cancelled.
      if (err instanceof Error && err.name === "AbortError") {
        smoother.flush()
        patch((m) => {
          const stoppedBlocks = (m.blocks ?? []).map((b) =>
            b.type === "tool" && b.activity.status === "running"
              ? {
                  ...b,
                  activity: {
                    ...b.activity,
                    status: "stopped" as const,
                    detail: b.activity.detail ?? "Stopped",
                  },
                }
              : b
          )
          return {
            ...withSyncedFields(m, stoppedBlocks),
            streaming: false,
            stopped: true,
          }
        })
      } else {
        smoother.flush()
        patch((m) => {
          const msg = formatApiError(err)
          const blocks = appendTextBlock(m.blocks ?? [], msg, newId, true)
          return { ...withSyncedFields(m, blocks), isError: true }
        })
      }
    } finally {
      abortControllerRef.current = null
      patch((m) => ({ ...m, streaming: false }))
      setIsLoading(false)
      completeRef.current?.()
    }
  }, [])

  /** Send whatever is in the composer, clearing it first. */
  const submit = React.useCallback(
    (text?: string) => {
      const value = text ?? inputRef.current
      if (!value.trim() || loadingRef.current) return
      setInput("")
      void send(value)
    },
    [send]
  )

  const reset = React.useCallback(() => {
    setMessages([])
    setInput("")
    conversationIdRef.current = null
  }, [])

  /** Adopt an existing thread — used when reopening stored history. */
  const adopt = React.useCallback(
    (conversationId: string | null, restored: ChatMessage[]) => {
      conversationIdRef.current = conversationId
      setMessages(restored)
    },
    []
  )

  const stopGeneration = React.useCallback(() => {
    abortControllerRef.current?.abort()
  }, [])

  return {
    messages,
    input,
    setInput,
    isLoading,
    submit,
    stopGeneration,
    reset,
    adopt,
    conversationId: conversationIdRef,
  }
}

export { newId as newChatId, formatApiError }
