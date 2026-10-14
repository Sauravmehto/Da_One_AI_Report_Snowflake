import { DatabaseIcon } from "lucide-react"

import type { DatabaseSummary } from "@/lib/api"
import { cn } from "@/lib/utils"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"

const CARD_PREVIEW_COUNT = 2

type ChatDatabasePickerProps = {
  databases: DatabaseSummary[]
  selected: DatabaseSummary | null
  onSelect: (database: DatabaseSummary) => void
  isLoading?: boolean
  error?: string | null
  disabled?: boolean
  className?: string
}

export function ChatDatabasePicker({
  databases,
  selected,
  onSelect,
  isLoading = false,
  error = null,
  disabled = false,
  className,
}: ChatDatabasePickerProps) {
  const cardDatabases = databases.slice(0, CARD_PREVIEW_COUNT)
  const hasOverflow = databases.length > CARD_PREVIEW_COUNT

  if (isLoading) {
    return (
      <div className={cn("w-full max-w-3xl space-y-2", className)}>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {Array.from({ length: CARD_PREVIEW_COUNT }).map((_, index) => (
            <Skeleton key={index} className="h-20 w-full rounded-lg" />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className={cn("w-full max-w-3xl space-y-4", className)}>
      <div>
        <p className="text-sm font-medium text-foreground">Select a database</p>
        <p className="text-sm text-muted-foreground">
          Choose which database this chat should use. This cannot be changed
          after you send your first message.
        </p>
      </div>

      {error ? <p className="text-sm text-destructive">{error}</p> : null}

      {databases.length === 0 && !error ? (
        <p className="text-sm text-muted-foreground">No databases available.</p>
      ) : (
        <>
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {cardDatabases.map((database) => {
              const isSelected = selected?.name === database.name

              return (
                <li key={database.name}>
                  <button
                    type="button"
                    disabled={disabled}
                    aria-pressed={isSelected}
                    onClick={() => onSelect(database)}
                    className={cn(
                      "flex h-full w-full items-start gap-3 rounded-lg border px-4 py-3 text-left transition-colors",
                      disabled && "cursor-not-allowed opacity-60",
                      !disabled && "hover:bg-muted/50",
                      isSelected && "border-primary bg-accent/40"
                    )}
                  >
                    <DatabaseIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium">
                        {database.display_name}
                      </p>
                      <p className="line-clamp-2 text-sm text-muted-foreground">
                        {database.description || "No description available."}
                      </p>
                    </div>
                  </button>
                </li>
              )
            })}
          </ul>

          {hasOverflow ? (
            <div className="space-y-2">
              <Label htmlFor="chat-database-select">More databases</Label>
              <Select
                value={selected?.name}
                onValueChange={(name) => {
                  const database = databases.find((db) => db.name === name)
                  if (database && !disabled) {
                    onSelect(database)
                  }
                }}
                disabled={disabled}
              >
                <SelectTrigger id="chat-database-select" className="w-full">
                  <SelectValue placeholder="Choose a database" />
                </SelectTrigger>
                <SelectContent>
                  {databases.map((database) => (
                    <SelectItem key={database.name} value={database.name}>
                      {database.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
