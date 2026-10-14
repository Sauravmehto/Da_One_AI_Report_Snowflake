import * as React from "react"
import { MessageSquareIcon, RotateCcwIcon, XIcon } from "lucide-react"

import { ChatComposer } from "@/components/chat/chat-composer"
import { ChatMessages } from "@/components/chat/chat-messages"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { useAgentChat, newChatId } from "@/hooks/use-agent-chat"
import { fetchConversation, fetchConversations } from "@/lib/api"
import { rehydrateConversation } from "@/lib/rehydrate-conversation"

/**
 * Read-only chat scoped to one saved dashboard.
 *
 * Answers from the dashboard's saved configuration (what each panel is, and
 * the SQL behind it) and from its live data, which the assistant fetches by
 * running the dashboard when a question actually needs figures. It cannot edit
 * the dashboard: the editing tools are withheld server-side, so this is a
 * property of the API and not just of the prompt.
 *
 * The thread belongs to the dashboard — it is reopened on return and stays out
 * of the sidebar's chat list.
 */

const SUGGESTIONS = [
  "What is this dashboard showing?",
  "Which panel has the biggest change?",
  "How is this calculated?",
]

export function DashboardChat({
  reportId,
  title,
  onClose,
}: {
  reportId: string
  title?: string | null
  onClose: () => void
}) {
  const scrollRef = React.useRef<HTMLDivElement>(null)

  const { messages, input, setInput, isLoading, submit, stopGeneration, reset, adopt } =
    useAgentChat({
      startWith: () => ({ dashboard_id: reportId }),
    })

  // Reopen the most recent thread for this dashboard. Scoped to the dashboard,
  // so this never pulls in a main-chat conversation.
  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const threads = await fetchConversations(reportId)
        if (cancelled || threads.length === 0) return
        const stored = await fetchConversation(threads[0].conversation_id)
        if (cancelled) return
        adopt(stored.conversation_id, rehydrateConversation(stored, newChatId))
      } catch {
        // A thread that will not load is not worth an error banner here —
        // the panel is still perfectly usable for a new question.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [reportId, adopt])

  // Follow the conversation as it streams.
  React.useEffect(() => {
    const viewport = scrollRef.current?.querySelector(
      "[data-slot=scroll-area-viewport]"
    )
    if (viewport) viewport.scrollTop = viewport.scrollHeight
  }, [messages, isLoading])

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b px-3 py-2">
        <MessageSquareIcon className="size-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">
            Ask about this dashboard
          </p>
          {title ? (
            <p className="truncate text-xs text-muted-foreground">{title}</p>
          ) : null}
        </div>
        {messages.length > 0 ? (
          <Button
            variant="ghost"
            size="icon"
            aria-label="Start a new thread"
            title="Start a new thread"
            onClick={reset}
          >
            <RotateCcwIcon className="size-4" />
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="icon"
          aria-label="Close chat"
          onClick={onClose}
        >
          <XIcon className="size-4" />
        </Button>
      </header>

      {/* Radix wraps the scroll content in a display:table box, which grows to
          fit its widest child — so one wide table in an answer stretched the
          whole column and it scrolled sideways. As a block it keeps the
          column's width, and wide content scrolls inside its own box. */}
      <ScrollArea
        ref={scrollRef}
        className="min-h-0 min-w-0 flex-1 [&_[data-slot=scroll-area-viewport]>div]:block!"
      >
        {messages.length === 0 ? (
          <div className="space-y-3 p-4">
            <p className="text-sm text-muted-foreground">
              Ask what a panel means, how a number is calculated, or what the
              data shows. Answers come from this dashboard's saved definition
              and its live data.
            </p>
            <div className="flex flex-col items-start gap-1.5">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => submit(suggestion)}
                  className="rounded-full border px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  {suggestion}
                </button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              This panel can read the dashboard but not change it. To edit it,
              ask in the main chat.
            </p>
          </div>
        ) : (
          // The shared transcript component is built around a centred reading
          // column; in a narrow side panel that column IS the panel, so its
          // max-width and outer padding are dropped here. `sm:px-3` is not
          // redundant — CHAT_COLUMN's own `sm:px-6` is a different variant and
          // would otherwise win back over `px-3` above 640px.
          <ChatMessages
            messages={messages}
            isLoading={isLoading}
            className="mx-0 max-w-none px-3 py-4 sm:px-3"
          />
        )}
      </ScrollArea>

      <div className="shrink-0 border-t p-3">
        <ChatComposer
          value={input}
          onChange={setInput}
          onSend={() => submit()}
          onStop={stopGeneration}
          isLoading={isLoading}
          placeholder="Ask about this dashboard…"
        />
      </div>
    </div>
  )
}
