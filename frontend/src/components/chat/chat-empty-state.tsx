import { FileSpreadsheetIcon } from "lucide-react"

import { ChatComposer } from "@/components/chat/chat-composer"
import { ChatDatabasePicker } from "@/components/chat/chat-database-picker"
import { CHAT_COLUMN } from "@/components/chat/chat-layout"
import { Button } from "@/components/ui/button"
import { useChat } from "@/contexts/chat-context"
import { useDatabase } from "@/contexts/database-context"
import type { DatabaseSummary } from "@/lib/api"
import { cn } from "@/lib/utils"

type ChatEmptyStateProps = {
  input: string
  onInputChange: (value: string) => void
  onSend: () => void
  onStop?: () => void
  isLoading?: boolean
  /** null clears the selection, returning the user to the picker. */
  onSelectDatabase: (database: DatabaseSummary | null) => void
}

export function ChatEmptyState({
  input,
  onInputChange,
  onSend,
  onStop,
  isLoading = false,
  onSelectDatabase,
}: ChatEmptyStateProps) {
  const { chatDatabase } = useChat()
  const {
    databases,
    isLoading: databasesLoading,
    error: databasesError,
  } = useDatabase()

  const hasDatabase = chatDatabase !== null
  // An uploaded source can only have arrived here preselected: it is never
  // picker's list, so there is no way to have chosen it on this screen.
  const isSourcePreselected = chatDatabase?.kind === "spreadsheet"

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="flex min-h-min flex-1 flex-col items-center justify-center gap-8 py-12">
        {/* A prompt, not a product name — the brand is already in the sidebar.
            Left deliberately unscoped: this surface answers questions, charts,
            explores and builds dashboards, so naming any one of them would
            undersell the rest. */}
        <div className="text-center">
          <h2 className="text-3xl font-semibold tracking-tight text-foreground">
            How can I help?
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            Ask a question, explore a table, or build a dashboard.
          </p>
        </div>

        {/* Database first, then the box you type in. Choosing the database is a
            precondition for asking anything, so it reads in the order it has to
            happen rather than leaving a disabled input at the top of the page.

            Unless the source is already decided — arriving from the
            Files library binds a source on the way in, and showing
            "Select a database" underneath it reads as though the choice still
            has to be made. Uploaded files are not in that list anyway, so the
            picker could not represent the selection even if it wanted to. */}
        <div className={cn(CHAT_COLUMN, "flex flex-col gap-6")}>
          {isSourcePreselected ? (
            <div className="flex flex-wrap items-center gap-2 rounded-lg border p-3">
              <FileSpreadsheetIcon className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium">
                  {chatDatabase.display_name}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {chatDatabase.description}
                </span>
              </span>
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto"
                onClick={() => onSelectDatabase(null)}
              >
                Change
              </Button>
            </div>
          ) : (
            <ChatDatabasePicker
              databases={databases}
              selected={chatDatabase}
              onSelect={onSelectDatabase}
              isLoading={databasesLoading}
              error={databasesError}
              className="max-w-none"
            />
          )}

          <ChatComposer
            value={input}
            onChange={onInputChange}
            onSend={onSend}
            onStop={onStop}
            isLoading={isLoading}
            canSend={hasDatabase}
            disabled={!hasDatabase}
            placeholder={
              hasDatabase
                ? `Ask anything about ${chatDatabase.display_name}…`
                : "Select a database above to start…"
            }
          />
        </div>
      </div>
    </div>
  )
}
