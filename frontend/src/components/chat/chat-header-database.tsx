import { DatabaseIcon } from "lucide-react"
import { useLocation } from "react-router-dom"

import { useChat } from "@/contexts/chat-context"

export function ChatHeaderDatabase() {
  const location = useLocation()
  const { chatDatabase } = useChat()

  if (location.pathname !== "/" || !chatDatabase) {
    return null
  }

  return (
    <div className="ml-auto flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
      <DatabaseIcon className="size-4 shrink-0" />
      <span className="truncate font-medium text-foreground">
        {chatDatabase.display_name}
      </span>
    </div>
  )
}
