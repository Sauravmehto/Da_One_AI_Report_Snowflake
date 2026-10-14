import * as React from "react"
import {
  ArrowLeftIcon,
  DatabaseIcon,
  SearchIcon,
  TableIcon,
} from "lucide-react"

import { useDatabase } from "@/contexts/database-context"
import { fetchTables, type DatabaseSummary, type TableSummary } from "@/lib/api"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { TableDetail } from "@/components/explore/table-detail"

function formatRowCount(count: number | null): string | null {
  if (count === null) {
    return null
  }
  return new Intl.NumberFormat().format(count)
}

function groupBySchema(tables: TableSummary[]): [string, TableSummary[]][] {
  const groups = new Map<string, TableSummary[]>()
  for (const table of tables) {
    const bucket = groups.get(table.schema) ?? []
    bucket.push(table)
    groups.set(table.schema, bucket)
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
}

export function ExploreView() {
  const { databases, isLoading: dbLoading } = useDatabase()

  const [tables, setTables] = React.useState<TableSummary[]>([])
  const [activeTable, setActiveTable] = React.useState<TableSummary | null>(
    null
  )
  const [search, setSearch] = React.useState("")
  const [isLoading, setIsLoading] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  // Explore keeps its OWN database selection, deliberately independent of the
  // chat's. A chat locks to one database for the life of the conversation
  // because its answers depend on that semantic layer; browsing has no such
  // constraint, and being forced to start a chat just to look at a table was
  // the wrong coupling.
  //
  // Always starts unset, so Explore opens on the database picker. It used to
  // resume the last database from localStorage, which meant the picker was
  // effectively unreachable after the first visit — you landed inside one
  // database with no way back out to see what else existed.
  const [databaseName, setDatabaseName] = React.useState<string | null>(null)

  // Undocumented databases are included on purpose: this is object browsing,
  // not AI querying, so anything the Snowflake role can see is fair game —
  // that is the point of not having to log into Snowflake for a quick look.
  const selected = databases.find((d) => d.name === databaseName) ?? null

  function chooseDatabase(name: string) {
    setDatabaseName(name)
    setActiveTable(null)
    setSearch("")
  }

  function backToDatabases() {
    setDatabaseName(null)
    setActiveTable(null)
    setSearch("")
  }

  React.useEffect(() => {
    if (!databaseName) {
      setTables([])
      setActiveTable(null)
      return
    }

    let isMounted = true
    setIsLoading(true)
    setError(null)
    setActiveTable(null)

    fetchTables(databaseName)
      .then((result) => {
        if (isMounted) {
          setTables(result)
        }
      })
      .catch((err: unknown) => {
        if (isMounted) {
          setError(err instanceof Error ? err.message : "Failed to load tables")
        }
      })
      .finally(() => {
        if (isMounted) {
          setIsLoading(false)
        }
      })

    return () => {
      isMounted = false
    }
  }, [databaseName])

  const filtered = React.useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) {
      return tables
    }
    return tables.filter(
      (table) =>
        table.name.toLowerCase().includes(query) ||
        table.description.toLowerCase().includes(query)
    )
  }, [tables, search])

  const grouped = React.useMemo(() => groupBySchema(filtered), [filtered])

  if (!databaseName) {
    return (
      <DatabasePicker
        databases={databases}
        isLoading={dbLoading}
        onChoose={chooseDatabase}
      />
    )
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
      {/* Left pane — table list */}
      <aside className="flex min-h-0 w-72 shrink-0 flex-col border-r">
        <div className="space-y-2 border-b p-3">
          {/* The way back to the picker. The Select below still switches
              database in place — that is the fast path once you know where you
              are going; this is for when you want to see what else there is. */}
          <button
            type="button"
            onClick={backToDatabases}
            className="flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            <ArrowLeftIcon className="size-3.5" />
            All databases
          </button>

          <Select value={databaseName} onValueChange={chooseDatabase}>
            <SelectTrigger className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {databases.map((db) => (
                <SelectItem key={db.name} value={db.name}>
                  {db.display_name || db.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <div className="relative">
            <SearchIcon className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search tables..."
              className="pl-8"
            />
          </div>
        </div>

        {/* overscroll-contain so hitting the end of the table list does not
            chain the scroll on to the pane behind it. */}
        <div className="scrollbar-slim min-h-0 flex-1 overflow-y-auto overscroll-contain p-2">
          {isLoading ? (
            <div className="space-y-2 p-1">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-9 w-full" />
              ))}
            </div>
          ) : error ? (
            <p className="p-2 text-sm text-destructive">{error}</p>
          ) : filtered.length === 0 ? (
            <p className="p-2 text-sm text-muted-foreground">
              {tables.length === 0 ? "No tables found." : "No matches."}
            </p>
          ) : (
            grouped.map(([schema, schemaTables]) => (
              <div key={schema} className="mb-3">
                <p className="px-2 py-1 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                  {schema}
                </p>
                <div className="space-y-0.5">
                  {schemaTables.map((table) => (
                    <button
                      key={table.full_name}
                      type="button"
                      onClick={() => setActiveTable(table)}
                      data-active={table.full_name === activeTable?.full_name}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors",
                        "hover:bg-muted data-[active=true]:bg-accent data-[active=true]:text-accent-foreground"
                      )}
                    >
                      <TableIcon className="size-4 shrink-0 text-muted-foreground" />
                      <span className="flex-1 truncate">{table.name}</span>
                      {table.is_documented ? (
                        <span
                          className="size-1.5 shrink-0 rounded-full bg-primary/60"
                          title="Documented"
                        />
                      ) : null}
                    </button>
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      </aside>

      {/* Right pane — detail or overview.
          A fixed frame, not a scroll container: TableDetail keeps its title and
          tabs pinned and scrolls the table inside them, so the column headers
          stay readable while you scan forty columns. The overview has nothing
          to pin, so it brings its own scroll. */}
      <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {activeTable && databaseName ? (
          <TableDetail
            // Keyed on the table: remounting resets tab choice and scroll
            // position, so picking a new table does not land you halfway down
            // the previous one's column list.
            key={activeTable.full_name}
            database={databaseName}
            schema={activeTable.schema}
            table={activeTable.name}
          />
        ) : (
          <div className="scrollbar-slim min-h-0 flex-1 overflow-y-auto overscroll-contain">
            <DatabaseOverview
              tableCount={tables.length}
              documentedCount={tables.filter((t) => t.is_documented).length}
              displayName={selected?.display_name || databaseName}
              description={selected?.description ?? ""}
              isDocumented={selected?.is_documented ?? false}
            />
          </div>
        )}
      </main>
    </div>
  )
}

/**
 * The landing view for Explore: every database the role can see, as tiles.
 *
 * Its own search box rather than the table search reused — the two filter
 * different things and sharing one would carry a table name into the database
 * list on the way back out.
 */
function DatabasePicker({
  databases,
  isLoading,
  onChoose,
}: {
  databases: DatabaseSummary[]
  isLoading: boolean
  onChoose: (name: string) => void
}) {
  const [query, setQuery] = React.useState("")

  const visible = React.useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return databases
    return databases.filter((db) =>
      `${db.display_name} ${db.name} ${db.description}`
        .toLowerCase()
        .includes(q)
    )
  }, [databases, query])

  return (
    <div className="scrollbar-slim min-h-0 flex-1 overflow-y-auto overscroll-contain">
      <div className="mx-auto w-full max-w-4xl space-y-5 p-6">
        <div>
          <h1 className="text-xl font-medium">Explore</h1>
          <p className="text-sm text-muted-foreground">
            Browse schemas, tables and columns without signing in to Snowflake.
            Pick a database to start.
          </p>
        </div>

        <div className="relative max-w-md">
          <SearchIcon className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search databases…"
            className="pl-8"
            autoFocus
          />
        </div>

        {/* Container queries so the tiles reflow on the width they actually
            have — the sidebar is resizable, which viewport breakpoints miss.
            The query element wraps every state, since the skeletons use the
            same grid and an element cannot query itself. */}
        <div className="@container/dbs">
          {isLoading ? (
            <div className="grid gap-3 @xl/dbs:grid-cols-2 @4xl/dbs:grid-cols-3">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-[104px] w-full" />
              ))}
            </div>
          ) : visible.length === 0 ? (
            <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
              {databases.length === 0
                ? "No databases are visible to your Snowflake role."
                : `No databases match “${query.trim()}”.`}
            </p>
          ) : (
            <div className="grid gap-3 @xl/dbs:grid-cols-2 @4xl/dbs:grid-cols-3">
              {visible.map((db) => (
                <button
                  key={db.name}
                  type="button"
                  onClick={() => onChoose(db.name)}
                  className="group flex min-w-0 flex-col rounded-xl border p-4 text-left transition-colors hover:border-foreground/20 hover:bg-muted/50"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <DatabaseIcon className="size-4 shrink-0 text-muted-foreground" />
                    <span className="truncate text-sm font-medium">
                      {db.display_name || db.name}
                    </span>
                    {db.is_documented ? (
                      <Badge
                        variant="secondary"
                        className="ml-auto shrink-0 text-[10px]"
                      >
                        Documented
                      </Badge>
                    ) : null}
                  </div>

                  <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                    {db.name}
                  </p>

                  <p className="mt-2 line-clamp-2 text-xs text-muted-foreground">
                    {db.description || "No description in the semantic layer."}
                  </p>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function DatabaseOverview({
  tableCount,
  documentedCount,
  displayName = "",
  description = "",
  isDocumented = false,
}: {
  tableCount: number
  documentedCount: number
  displayName?: string
  description?: string
  isDocumented?: boolean
}) {
  return (
    // Left-aligned with the same padding TableDetail uses, so the heading does
    // not jump horizontally when you select a table and come back.
    <div className="w-full max-w-3xl p-6">
      <div className="flex items-center gap-2">
        <DatabaseIcon className="size-5 shrink-0 text-muted-foreground" />
        <h2 className="truncate text-lg font-semibold">{displayName}</h2>
        {isDocumented ? <Badge variant="secondary">Documented</Badge> : null}
      </div>

      {description ? (
        <p className="mt-2 text-sm text-muted-foreground">{description}</p>
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">
          No description available for this database yet.
        </p>
      )}

      <div className="mt-6 flex gap-3">
        <div className="rounded-lg border px-4 py-3">
          <p className="text-2xl font-semibold">{tableCount}</p>
          <p className="text-xs text-muted-foreground">Tables</p>
        </div>
        <div className="rounded-lg border px-4 py-3">
          <p className="text-2xl font-semibold">{documentedCount}</p>
          <p className="text-xs text-muted-foreground">Documented</p>
        </div>
      </div>

      <p className="mt-6 text-sm text-muted-foreground">
        Select a table on the left to see its columns, business definitions, and
        a preview of the data.
      </p>
    </div>
  )
}

export { formatRowCount }
