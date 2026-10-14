import * as React from "react"
import {
  ChevronDownIcon,
  RotateCcwIcon,
  SparklesIcon,
  Undo2Icon,
} from "lucide-react"

import { ChatComposer } from "@/components/chat/chat-composer"
import {
  ChatMessages,
  type ChatMessage,
} from "@/components/chat/chat-messages"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  assistDashboardEdit,
  fetchConversation,
  fetchConversations,
  noteDashboardAssist,
  type DashboardAssistResult,
  type DashboardDraft,
} from "@/lib/api"
import { rehydrateConversation } from "@/lib/rehydrate-conversation"

const SUGGESTIONS = [
  "Which chart type suits each panel best?",
  "What else would be worth adding to this dashboard?",
  "Show values on the bars and hide the legend",
]

function newId(): string {
  return Math.random().toString(36).slice(2)
}

function readDetail(err: unknown): string {
  if (!(err instanceof Error)) return "Something went wrong. Please try again."
  try {
    const parsed = JSON.parse(err.message) as { detail?: unknown }
    if (typeof parsed.detail === "string") return parsed.detail
  } catch {
    // Not JSON: the message is already text.
  }
  return err.message || "Something went wrong. Please try again."
}

/**
 * The assistant docked at the bottom of the Edit panel.
 *
 * It changes the DRAFT the form holds — never the saved dashboard. What it
 * changed shows up in the form straight away, and saving stays with the panel's
 * own "Save and refresh". The thread is kept per person and dashboard, so
 * reopening the panel shows what earlier messages changed and which draft was
 * saved as which version.
 */
export function DashboardEditAssistant({
  reportId,
  open,
  getDraft,
  onResult,
  canUndo,
  onUndo,
  unsavedChanges,
  onConversation,
  onCollapse,
}: {
  reportId: string
  /** Whether the dock is showing. It stays mounted while hidden, and a hidden
   *  list has no height to scroll, so opening is when it scrolls to the end. */
  open: boolean
  /** The draft as it stands at the moment a message is sent. */
  getDraft: () => DashboardDraft
  /** Called with every answer. Returns false when the answer could not be
   *  applied because the draft changed while the assistant was working. */
  onResult: (result: DashboardAssistResult, sentDraft: DashboardDraft, request: string) => boolean
  canUndo: boolean
  onUndo: () => void
  /** Assistant changes in the draft that have not been saved yet. */
  unsavedChanges: number
  onConversation: (conversationId: string | null) => void
  onCollapse: () => void
}) {
  const [messages, setMessages] = React.useState<ChatMessage[]>([])
  const [input, setInput] = React.useState("")
  const [isLoading, setIsLoading] = React.useState(false)
  const conversationRef = React.useRef<string | null>(null)
  const abortRef = React.useRef<AbortController | null>(null)
  const scrollRef = React.useRef<HTMLDivElement>(null)

  // Reopen this dashboard's most recent edit thread.
  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const threads = await fetchConversations(reportId, "dashboard_edit")
        if (cancelled || threads.length === 0) return
        const stored = await fetchConversation(threads[0].conversation_id)
        if (cancelled) return
        conversationRef.current = stored.conversation_id
        onConversation(stored.conversation_id)
        setMessages(rehydrateConversation(stored, newId))
      } catch {
        // A thread that will not load still leaves the assistant usable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [reportId, onConversation])

  React.useEffect(() => () => abortRef.current?.abort(), [])

  // Follow the conversation, and show its end when the dock opens.
  React.useEffect(() => {
    if (!open) return
    const viewport = scrollRef.current?.querySelector(
      "[data-slot=scroll-area-viewport]"
    )
    if (viewport) viewport.scrollTop = viewport.scrollHeight
  }, [messages, open, unsavedChanges])

  /** Show a note in the transcript and keep it in the thread. */
  const recordNote = React.useCallback(
    (event: "undo" | "not_applied", text: string) => {
      setMessages((current) => [...current, { id: newId(), role: "assistant", content: text }])
      const conversationId = conversationRef.current
      if (conversationId) {
        void noteDashboardAssist(reportId, conversationId, event).catch(() => {
          // The draft is already right; a thread that misses the note is not
          // worth interrupting the person over.
        })
      }
    },
    [reportId]
  )

  const send = React.useCallback(
    async (text?: string) => {
      const request = (text ?? input).trim()
      if (!request || isLoading) return
      const draft = getDraft()
      const pendingId = newId()
      setInput("")
      setIsLoading(true)
      setMessages((current) => [
        ...current,
        { id: newId(), role: "user", content: request },
        {
          id: pendingId,
          role: "assistant",
          content: "",
          streaming: true,
          thinking: "Working on the draft and checking the queries…",
        },
      ])

      const controller = new AbortController()
      abortRef.current = controller
      try {
        const result = await assistDashboardEdit(
          reportId,
          { message: request, plan: draft, conversation_id: conversationRef.current },
          controller.signal
        )
        conversationRef.current = result.conversation_id
        onConversation(result.conversation_id)
        const applied = onResult(result, draft, request)
        setMessages((current) =>
          current.map((m) =>
            m.id === pendingId ? { id: pendingId, role: "assistant", content: result.message } : m
          )
        )
        if (!applied) {
          recordNote(
            "not_applied",
            "That change was not applied: the draft was edited while it was being prepared. The draft is as it was before this request."
          )
        }
      } catch (err) {
        const stopped = controller.signal.aborted
        setMessages((current) =>
          current.map((m) =>
            m.id === pendingId
              ? {
                  id: pendingId,
                  role: "assistant",
                  content: stopped ? "Stopped. The draft was not changed." : readDetail(err),
                  isError: !stopped,
                  stopped,
                }
              : m
          )
        )
      } finally {
        if (abortRef.current === controller) abortRef.current = null
        setIsLoading(false)
      }
    },
    [input, isLoading, getDraft, reportId, onResult, onConversation, recordNote]
  )

  function startNewThread() {
    abortRef.current?.abort()
    conversationRef.current = null
    onConversation(null)
    setMessages([])
  }

  return (
    <section
      aria-label="Edit with AI"
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-violet-500/30 bg-background shadow-sm"
    >
      <header className="flex shrink-0 items-center gap-1 border-b border-violet-500/20 bg-violet-500/[0.07] px-3 py-1.5">
        <SparklesIcon className="size-4 shrink-0 text-violet-500" />
        <p className="min-w-0 flex-1 truncate text-sm font-medium">Edit with AI</p>
        {canUndo ? (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2 text-xs"
            onClick={() => {
              onUndo()
              recordNote("undo", "Undid the last change to the draft.")
            }}
            disabled={isLoading}
            title="Undo the assistant's last change to the draft"
          >
            <Undo2Icon className="size-3.5" />
            Undo
          </Button>
        ) : null}
        {messages.length > 0 ? (
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            aria-label="Start a new thread"
            title="Start a new thread"
            onClick={startNewThread}
          >
            <RotateCcwIcon className="size-3.5" />
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="icon"
          className="size-7"
          aria-label="Hide the assistant"
          title="Hide the assistant"
          onClick={onCollapse}
        >
          <ChevronDownIcon className="size-4" />
        </Button>
      </header>

      <ScrollArea
        ref={scrollRef}
        className="min-h-0 min-w-0 flex-1 [&_[data-slot=scroll-area-viewport]>div]:block!"
      >
        {messages.length === 0 ? (
          <div className="space-y-3 p-3">
            <p className="text-xs text-muted-foreground">
              Describe a change, or ask for help deciding. Changes go into the
              draft above; nothing is saved until you press Save and refresh.
            </p>
            <div className="flex flex-col items-start gap-1.5">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => void send(suggestion)}
                  className="rounded-full border bg-background px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <ChatMessages
            messages={messages}
            className="mx-0 max-w-none gap-4 px-3 py-3 text-sm sm:px-3"
          />
        )}
        {/* The draft is not the dashboard. Said at the end of the chat, every
            time there is something unsaved, because a reply saying "changed"
            reads as done. */}
        {unsavedChanges > 0 && !isLoading ? (
          <p className="mx-3 mb-3 flex items-start gap-2 rounded-lg border border-violet-500/30 bg-violet-500/[0.07] px-3 py-2 text-xs text-foreground">
            <SparklesIcon className="mt-0.5 size-3.5 shrink-0 text-violet-500" />
            <span>
              {unsavedChanges === 1 ? "This change is" : `These ${unsavedChanges} changes are`} in
              the draft only. Press <span className="font-semibold">Save and refresh</span> below
              to save {unsavedChanges === 1 ? "it" : "them"} and see the dashboard update.
            </span>
          </p>
        ) : null}
      </ScrollArea>

      <div className="shrink-0 border-t bg-background p-2">
        <ChatComposer
          value={input}
          onChange={setInput}
          onSend={() => void send()}
          onStop={() => abortRef.current?.abort()}
          isLoading={isLoading}
          placeholder="Change a panel, add one, or ask what would work…"
        />
      </div>
    </section>
  )
}
