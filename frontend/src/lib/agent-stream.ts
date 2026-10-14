/**
 * SSE client for the streaming agent endpoint.
 *
 * Not EventSource: that only does GET, and the request carries a message body
 * plus session cookies. So it's fetch + a manual reader over the response
 * stream, parsing SSE frames as they arrive.
 */

export type AgentEvent =
  | { type: "message_start"; data: { conversation_id: string } }
  | { type: "text"; data: { delta: string } }
  /** A summary of the model's reasoning, streamed while it works. Adaptive
   *  thinking can run for a long time before any text, and without this the
   *  turn looked frozen. */
  | { type: "thinking"; data: { delta: string } }
  | { type: "tool_start"; data: { id: string; name: string; input?: unknown } }
  | {
      type: "tool_end"
      data: {
        id: string
        name: string
        ok: boolean
        detail?: string | null
        truncated?: boolean
      }
    }
  /** A renderable result (currently charts). Carries the FULL payload — the
   *  model only received a summary of it. */
  | {
      type: "artifact"
      data: { id: string; kind: "chart"; panel: Record<string, unknown> }
    }
  | { type: "message_stop"; data: { stop_reason: string } }
  | { type: "error"; data: { message: string } }

export type AgentRequestBody = {
  message: string
  database?: string
  conversation_id?: string
  /** Scopes a NEW thread to one saved dashboard. The server takes the database
   *  from that dashboard's plan, so `database` is not sent alongside it. */
  dashboard_id?: string
}

/** One SSE frame: `event: <name>` then `data: <json>`, blank line terminated. */
function parseFrame(raw: string): AgentEvent | null {
  let event: string | null = null
  const dataLines: string[] = []

  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim()
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim())
    }
  }

  if (!event || dataLines.length === 0) {
    return null
  }

  try {
    return { type: event, data: JSON.parse(dataLines.join("\n")) } as AgentEvent
  } catch {
    // A frame we can't parse shouldn't kill the stream.
    return null
  }
}

export async function streamAgentChat(
  body: AgentRequestBody,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetch("/api/v1/agent/chat", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  })

  if (!response.ok || !response.body) {
    // Errors before the stream opens come back as ordinary JSON.
    const text = await response.text()
    let detail = text
    try {
      detail = (JSON.parse(text) as { detail?: string }).detail ?? text
    } catch {
      // keep raw text
    }
    throw new Error(detail || `Request failed (${response.status})`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      // Frames are blank-line separated; the tail may be a partial frame, so
      // it stays in the buffer until the rest of it arrives.
      let split = buffer.indexOf("\n\n")
      while (split !== -1) {
        const frame = buffer.slice(0, split)
        buffer = buffer.slice(split + 2)
        const parsed = parseFrame(frame)
        if (parsed) onEvent(parsed)
        split = buffer.indexOf("\n\n")
      }
    }
  } finally {
    reader.releaseLock()
  }
}
