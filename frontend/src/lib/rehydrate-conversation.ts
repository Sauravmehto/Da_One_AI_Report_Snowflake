import type {
  AssistantBlock,
  ChatMessage,
} from "@/components/chat/chat-messages"
import { withSyncedFields } from "@/components/chat/chat-messages"
import type { ToolActivity } from "@/components/chat/tool-activity"
import type { StoredConversation } from "@/lib/api"

/**
 * Stored Anthropic content blocks -> displayable messages.
 *
 * The stored shape is the model's, not the UI's: an assistant turn may hold
 * thinking + tool_use blocks, and tool RESULTS come back as a "user" turn that
 * the person never typed. Rendering that raw would show the user talking to
 * themselves in JSON, so tool results are folded into the preceding assistant
 * message's activity list instead.
 *
 * Shared by the main chat and the dashboard side panel — both reopen stored
 * threads, and two copies of this would disagree the first time the stored
 * shape changed.
 */
/**
 * A paragraph break between two text blocks of one assistant turn.
 *
 * The model emits a separate text block either side of a tool call, and joining
 * them with nothing glues the second onto the end of the first line. That is
 * invisible for prose but destroys any block-level markdown that has to start
 * at a line boundary — a table rendered as "…something else.| Lease Year |" is
 * parsed as a paragraph full of pipes, not a table.
 *
 * Idempotent about newlines the block already ends with, so a block that came
 * with its own trailing break does not gain a blank line.
 */
export function ensureParagraphBreak(content: string): string {
  if (!content) return content
  if (content.endsWith("\n\n")) return content
  if (content.endsWith("\n")) return content + "\n"
  return content + "\n\n"
}

export function rehydrateConversation(
  stored: StoredConversation,
  newId: () => string
): ChatMessage[] {
  const out: ChatMessage[] = []

  for (const message of stored.messages) {
    const blocks = message.content ?? []
    const isToolResultTurn = blocks.some((b) => b.type === "tool_result")

    if (message.role === "user" && isToolResultTurn) {
      // Mark the tools on the previous assistant message as finished.
      const previous = out[out.length - 1]
      if (previous?.blocks) {
        const updated = previous.blocks.map((block) => {
          if (block.type !== "tool") return block
          const result = blocks.find((b) => b.tool_use_id === block.activity.id)
          if (!result) return block
          return {
            ...block,
            activity: {
              ...block.activity,
              status: (result.is_error ? "error" : "ok") as ToolActivity["status"],
            },
          }
        })
        Object.assign(previous, withSyncedFields(previous, updated))
      } else if (previous?.tools) {
        previous.tools = previous.tools.map((t) => {
          const result = blocks.find((b) => b.tool_use_id === t.id)
          if (!result) return t
          return { ...t, status: result.is_error ? "error" : "ok" }
        })
      }
      continue
    }

    // Walk Anthropic blocks in order so restored chats keep the same
    // text → tool → text chronology as the live stream.
    const uiBlocks: AssistantBlock[] = []
    for (const b of blocks) {
      if (b.type === "text") {
        const text = String(b.text ?? "")
        if (!text) continue
        uiBlocks.push({ type: "text", id: newId(), content: text })
        continue
      }
      if (b.type === "tool_use") {
        const toolId = String(b.id ?? newId())
        const activity: ToolActivity = {
          id: toolId,
          name: String(b.name ?? "tool"),
          status: "ok",
          input: b.input,
        }
        uiBlocks.push({ type: "tool", id: toolId, activity })

        const artifact = stored.artifacts?.[toolId]
        if (artifact?.kind === "chart") {
          uiBlocks.push({
            type: "chart",
            id: newId(),
            panel: artifact.panel as unknown as NonNullable<
              ChatMessage["charts"]
            >[number],
          })
        }
      }
    }

    if (uiBlocks.length === 0) continue

    const base: ChatMessage = {
      id: newId(),
      role: message.role,
      content: "",
    }
    out.push(withSyncedFields(base, uiBlocks))
  }

  return out
}
