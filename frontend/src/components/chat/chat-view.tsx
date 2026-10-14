import * as React from "react"
import { useLocation, useNavigate } from "react-router-dom"

import { ChatComposer } from "@/components/chat/chat-composer"
import { ChatEmptyState } from "@/components/chat/chat-empty-state"
import { CHAT_COLUMN } from "@/components/chat/chat-layout"
import {
  ChatMessages,
  useScrollToBottom,
} from "@/components/chat/chat-messages"
import { useChat } from "@/contexts/chat-context"
import { useConversations } from "@/contexts/conversations-context"
import { useDatabase } from "@/contexts/database-context"
import { newChatId, useAgentChat } from "@/hooks/use-agent-chat"
import { fetchConversation, type DatabaseSummary } from "@/lib/api"
import { isCollectionName, isSpreadsheetName } from "@/lib/excel-source-api"
import { rehydrateConversation } from "@/lib/rehydrate-conversation"
import { ScrollArea } from "@/components/ui/scroll-area"

type ChatLocationState = {
  newChat?: boolean
  prompt?: string
  database?: string
  resumeConversationId?: string
}

export function ChatView() {
  const location = useLocation()
  const navigate = useNavigate()
  const { databases, resolveSource } = useDatabase()
  const { chatDatabase, setChatDatabase } = useChat()
  const { refresh: refreshConversations, setActiveId } = useConversations()

  const scrollRef = React.useRef<HTMLDivElement>(null)
  const autoSentRef = React.useRef(false)

  // The database a new thread starts against. Mirrored into a ref because the
  // auto-send path chooses it and sends in the same tick, before a setState
  // has landed — reading component state there would send the previous one.
  const databaseRef = React.useRef<DatabaseSummary | null>(chatDatabase)
  React.useEffect(() => {
    databaseRef.current = chatDatabase
  }, [chatDatabase])

  const { messages, input, setInput, isLoading, submit, stopGeneration, reset, adopt } =
    useAgentChat({
      startWith: () => {
        const database = databaseRef.current
        return database ? { database: database.name } : null
      },
      onConversationStarted: (conversationId, isNew) => {
        setActiveId(conversationId)
        // A brand-new conversation needs to appear in the sidebar now, not on
        // the next mount.
        if (isNew) void refreshConversations()
      },
      // Title and message count change with every turn.
      onTurnComplete: () => void refreshConversations(),
    })

  const isDatabaseLocked = messages.length > 0
  const hasMessages = messages.length > 0

  useScrollToBottom([messages, isLoading], scrollRef)

  React.useEffect(() => {
    const state = location.state as ChatLocationState | null

    if (state?.newChat) {
      reset()
      setChatDatabase(null)
      databaseRef.current = null
      autoSentRef.current = false
      setActiveId(null)
      navigate(location.pathname, { replace: true, state: null })
      return
    }

    if (state?.resumeConversationId) {
      const id = state.resumeConversationId
      navigate(location.pathname, { replace: true, state: null })
      autoSentRef.current = true // a resumed thread must not re-fire a prompt
      void (async () => {
        try {
          const stored = await fetchConversation(id)
          adopt(
            stored.conversation_id,
            rehydrateConversation(stored, newChatId)
          )
          setActiveId(stored.conversation_id)
          // Resolved rather than looked up: a resumed spreadsheet thread has
          // only its stored source name, and spreadsheets are not in the picker.
          const db = await resolveSource(stored.database ?? "")
          if (db) {
            setChatDatabase(db)
            databaseRef.current = db
          }
        } catch {
          adopt(null, [
            {
              id: newChatId(),
              role: "assistant",
              content: "That conversation could not be loaded.",
              isError: true,
            },
          ])
        }
      })()
      return
    }

    // Binding a source and sending a prompt are separate: the Spreadsheets
    // library opens a thread against a workbook with NO question attached, so
    // requiring a prompt here left the source unbound and the picker showing.
    if (autoSentRef.current || !state?.database) {
      return
    }

    // A warehouse name needs the list loaded to resolve; a spreadsheet or a
    // folder does not, because both are fetched by id rather than looked up.
    if (
      databases.length === 0 &&
      !isSpreadsheetName(state.database) &&
      !isCollectionName(state.database)
    ) {
      return
    }

    // Claim this before the await: the effect re-runs when the resolved source
    // lands, and without the guard a prompt would be submitted twice — the
    // second submit aborting the first mid-stream.
    autoSentRef.current = true
    const pendingPrompt = state.prompt
    const requested = state.database
    navigate(location.pathname, { replace: true, state: null })

    void (async () => {
      const source = await resolveSource(requested)
      if (!source) {
        autoSentRef.current = false
        return
      }
      setChatDatabase(source)
      databaseRef.current = source
      if (pendingPrompt) submit(pendingPrompt)
    })()
  }, [
    location.state,
    location.pathname,
    databases,
    navigate,
    submit,
    reset,
    adopt,
    setChatDatabase,
    setActiveId,
  ])

  /** null clears the selection — "Change" on a preselected spreadsheet. */
  function handleSelectDatabase(database: DatabaseSummary | null) {
    if (!isDatabaseLocked) {
      setChatDatabase(database)
      databaseRef.current = database
    }
  }

  function handleSend() {
    if (!chatDatabase) return
    submit()
  }

  if (!hasMessages) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <ChatEmptyState
          input={input}
          onInputChange={setInput}
          onSend={handleSend}
          onStop={stopGeneration}
          isLoading={isLoading}
          onSelectDatabase={handleSelectDatabase}
        />
      </div>
    )
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background">
      {/* Radix wraps the scroll content in a display:table box, which grows to
          fit its widest child — so one wide table in an answer stretched the
          whole column and it scrolled sideways. As a block it keeps the
          column's width, and wide content scrolls inside its own box. */}
      <ScrollArea
        ref={scrollRef}
        className="min-h-0 min-w-0 flex-1 [&_[data-slot=scroll-area-viewport]>div]:block!"
      >
        <ChatMessages messages={messages} isLoading={isLoading} />
      </ScrollArea>

      <div className="shrink-0 border-t border-border bg-background py-4">
        <ChatComposer
          value={input}
          onChange={setInput}
          onSend={handleSend}
          onStop={stopGeneration}
          isLoading={isLoading}
          canSend={Boolean(chatDatabase)}
          placeholder="Ask a follow-up"
          className={CHAT_COLUMN}
        />
      </div>
    </div>
  )
}
