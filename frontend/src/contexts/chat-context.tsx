/* eslint-disable react-refresh/only-export-components */
import * as React from "react"

import type { DatabaseSummary } from "@/lib/api"

type ChatContextValue = {
  chatDatabase: DatabaseSummary | null
  setChatDatabase: (database: DatabaseSummary | null) => void
  isDatabaseLocked: boolean
  setIsDatabaseLocked: (locked: boolean) => void
}

const ChatContext = React.createContext<ChatContextValue | undefined>(undefined)

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const [chatDatabase, setChatDatabase] =
    React.useState<DatabaseSummary | null>(null)
  const [isDatabaseLocked, setIsDatabaseLocked] = React.useState(false)

  const value = React.useMemo<ChatContextValue>(
    () => ({
      chatDatabase,
      setChatDatabase,
      isDatabaseLocked,
      setIsDatabaseLocked,
    }),
    [chatDatabase, isDatabaseLocked]
  )

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>
}

export function useChat() {
  const context = React.useContext(ChatContext)
  if (context === undefined) {
    throw new Error("useChat must be used within a ChatProvider")
  }
  return context
}
