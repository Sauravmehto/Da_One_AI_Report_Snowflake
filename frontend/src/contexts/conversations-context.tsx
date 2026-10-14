/* eslint-disable react-refresh/only-export-components */
import * as React from "react"

import {
  deleteConversation as apiDelete,
  updateConversation as apiUpdate,
  fetchConversations,
  type ConversationSummary,
} from "@/lib/api"

type ConversationsContextValue = {
  conversations: ConversationSummary[]
  /** Re-fetch the list — called when a turn creates or updates a conversation. */
  refresh: () => Promise<void>
  remove: (id: string) => Promise<void>
  rename: (id: string, title: string) => Promise<void>
  togglePin: (id: string) => Promise<void>
  /** The conversation currently open in the chat view, if any. */
  activeId: string | null
  setActiveId: (id: string | null) => void
}

const ConversationsContext = React.createContext<
  ConversationsContextValue | undefined
>(undefined)

export function ConversationsProvider({
  children,
}: {
  children: React.ReactNode
}) {
  const [conversations, setConversations] = React.useState<
    ConversationSummary[]
  >([])
  const [activeId, setActiveId] = React.useState<string | null>(null)

  const refresh = React.useCallback(async () => {
    try {
      // Awaited before any setState so this is safe to call straight from an
      // effect without triggering a synchronous cascading render.
      const list = await fetchConversations()
      setConversations(list)
    } catch {
      // Unauthenticated or offline — leave the list as-is rather than
      // blanking history the user can still see.
    }
  }, [])

  const remove = React.useCallback(
    async (id: string) => {
      await apiDelete(id)
      setConversations((prev) => prev.filter((c) => c.conversation_id !== id))
      setActiveId((current) => (current === id ? null : current))
    },
    []
  )

  const rename = React.useCallback(async (id: string, title: string) => {
    const updated = await apiUpdate(id, { title })
    setConversations((prev) =>
      prev.map((c) => (c.conversation_id === id ? { ...c, title: updated.title } : c))
    )
  }, [])

  const togglePin = React.useCallback(async (id: string) => {
    const current = conversations.find((c) => c.conversation_id === id)
    if (!current) return
    const updated = await apiUpdate(id, { pinned: !current.pinned })
    setConversations((prev) => {
      const next = prev.map((c) =>
        c.conversation_id === id ? { ...c, pinned: updated.pinned } : c
      )
      // Re-sort: pinned first, then by updated_at desc
      next.sort((a, b) => {
        if (a.pinned !== b.pinned) return a.pinned ? -1 : 1
        return b.updated_at.localeCompare(a.updated_at)
      })
      return next
    })
  }, [conversations])

  React.useEffect(() => {
    void refresh()
  }, [refresh])

  const value = React.useMemo<ConversationsContextValue>(
    () => ({ conversations, refresh, remove, rename, togglePin, activeId, setActiveId }),
    [conversations, refresh, remove, rename, togglePin, activeId]
  )

  return (
    <ConversationsContext.Provider value={value}>
      {children}
    </ConversationsContext.Provider>
  )
}

export function useConversations() {
  const context = React.useContext(ConversationsContext)
  if (context === undefined) {
    throw new Error(
      "useConversations must be used within a ConversationsProvider"
    )
  }
  return context
}
