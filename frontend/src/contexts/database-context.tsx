/* eslint-disable react-refresh/only-export-components */
import * as React from "react"

import { fetchDatabases, type DatabaseSummary } from "@/lib/api"
import {
  fetchDataset,
  isCollectionName,
  isSpreadsheetName,
  listCollections,
  sourceIdFromName,
} from "@/lib/excel-source-api"

type DatabaseContextValue = {
  /** Warehouse databases, for the chat picker. Spreadsheets are NOT here. */
  databases: DatabaseSummary[]
  isLoading: boolean
  error: string | null
  refresh: () => Promise<void>
  /**
   * The source behind a conversation's `database` name, whichever kind it is.
   *
   * Chat needs to name the source it is querying — including for a resumed
   * thread, where all it has is the stored name. A warehouse is found in the
   * list above; an uploaded spreadsheet is fetched on demand, because it is
   * deliberately not in that list.
   */
  resolveSource: (name: string) => Promise<DatabaseSummary | null>
}

const DatabaseContext = React.createContext<DatabaseContextValue | undefined>(
  undefined
)

export function DatabaseProvider({ children }: { children: React.ReactNode }) {
  const [databases, setDatabases] = React.useState<DatabaseSummary[]>([])
  const [isLoading, setIsLoading] = React.useState(true)
  const [error, setError] = React.useState<string | null>(null)

  const load = React.useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      // Warehouses ONLY. Uploaded spreadsheets deliberately do not appear here:
      // a firm has a dozen databases but will accumulate hundreds of
      // spreadsheets, and merging them turns a short, stable picker into an
      // unusable list. They live in their own library and reach chat through
      // resolveSource below.
      const result = await fetchDatabases()
      setDatabases(result.map((db) => ({ ...db, kind: "warehouse" as const })))
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load databases")
    } finally {
      setIsLoading(false)
    }
  }, [])

  React.useEffect(() => {
    void load()
  }, [load])

  // Spreadsheets already resolved this session. A thread's source is looked up
  // on every render pass that touches it, and re-fetching an immutable record
  // each time would put a request behind every keystroke.
  const spreadsheetCache = React.useRef(new Map<string, DatabaseSummary>())

  const resolveSource = React.useCallback(
    async (name: string): Promise<DatabaseSummary | null> => {
      if (!name) return null
      if (isCollectionName(name)) {
        const cached = spreadsheetCache.current.get(name)
        if (cached) return cached

        // A folder is not in any list the picker shows, so it is looked up by
        // id the same way a spreadsheet is.
        const id = name.slice(name.indexOf(":") + 1)
        const collection = (await listCollections()).find(
          (c) => c.collection_id === id
        )
        if (!collection) return null
        const documents = collection.documents ?? []
        const parts = [
          `${collection.datasets.length} dataset${collection.datasets.length === 1 ? "" : "s"}`,
        ]
        if (documents.length > 0) {
          parts.push(
            `${documents.length} document${documents.length === 1 ? "" : "s"}`
          )
        }
        parts.push(`${collection.total_rows.toLocaleString()} rows`)
        const folder: DatabaseSummary = {
          name,
          display_name: collection.name,
          description: parts.join(" · "),
          is_documented: true,
          kind: "spreadsheet",
        }
        spreadsheetCache.current.set(name, folder)
        return folder
      }
      if (!isSpreadsheetName(name)) {
        return databases.find((db) => db.name === name) ?? null
      }

      const cached = spreadsheetCache.current.get(name)
      if (cached) return cached

      const source = await fetchDataset(sourceIdFromName(name))
      if (!source) return null
      const summary: DatabaseSummary = {
        name,
        display_name: source.name,
        description: `${source.file_count} file${source.file_count === 1 ? "" : "s"} · ${source.total_rows.toLocaleString()} rows · ${source.tables.length} tables`,
        is_documented: true,
        kind: "spreadsheet",
      }
      spreadsheetCache.current.set(name, summary)
      return summary
    },
    [databases]
  )

  const value = React.useMemo<DatabaseContextValue>(
    () => ({
      databases,
      isLoading,
      error,
      refresh: load,
      resolveSource,
    }),
    [databases, isLoading, error, load, resolveSource]
  )

  return (
    <DatabaseContext.Provider value={value}>
      {children}
    </DatabaseContext.Provider>
  )
}

export function useDatabase() {
  const context = React.useContext(DatabaseContext)
  if (context === undefined) {
    throw new Error("useDatabase must be used within a DatabaseProvider")
  }
  return context
}
