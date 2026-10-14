import * as React from "react"
import { Link } from "react-router-dom"
import {
  ChevronDownIcon,
  ChevronRightIcon,
  FolderIcon,
  FolderInputIcon,
  LayoutDashboardIcon,
  SearchIcon,
  Trash2Icon,
} from "lucide-react"
import { accent } from "@/lib/accents"
import { toast } from "sonner"

import { DeleteDialog } from "@/components/delete-dialog"
import { useDeleteTarget } from "@/hooks/use-delete-target"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { useAuth } from "@/contexts/auth-context"
import {
  deleteDashboard,
  fetchSavedDashboards,
  setDashboardGroup,
  type SavedDashboardSummary,
} from "@/lib/api"
import { cn } from "@/lib/utils"
import { parseUtcTimestamp } from "@/lib/time"

/**
 * The table's columns, in order.
 *
 * `width` is the starting width in pixels; the header lets you drag it. The
 * table is laid out from these rather than from its content, so a long title
 * cannot push the date columns out of shape.
 */
type ColumnKey =
  | "title"
  | "editor"
  | "edited"
  | "runs"
  | "lastRun"

type Column = {
  key: ColumnKey
  label: string
  width: number
  align?: "right"
  value: (d: SavedDashboardSummary) => string | number
}

const COLUMNS: Column[] = [
  { key: "title", label: "Title", width: 420,
    value: (d) => d.report_title.toLowerCase() },
  { key: "editor", label: "Last edited by", width: 120,
    value: (d) => (d.updated_by ?? "").toLowerCase() },
  { key: "edited", label: "Edited", width: 150,
    value: (d) => d.updated_at ?? "" },
  { key: "runs", label: "Runs", width: 70, align: "right",
    value: (d) => d.run_count },
  { key: "lastRun", label: "Last run", width: 150,
    value: (d) => d.last_run_at ?? "" },
]

const MIN_COLUMN_WIDTH = 60

/** The row-actions column. Not sortable, so it is not one of COLUMNS. */
const ACTIONS_WIDTH = 72

/** The default widths above are PROPORTIONS, not pixels.
 *
 *  Used as pixels they total more than the page is wide, so the table arrived
 *  with a scrollbar before anyone had asked for one. Until a column is
 *  resized they are turned into percentages of whatever width is available,
 *  which always fits exactly; from the first drag onward they become real
 *  pixels and a scrollbar appears only if the widths outgrow the space. */
const TOTAL_DEFAULT_WIDTH =
  COLUMNS.reduce((total, column) => total + column.width, 0) + ACTIONS_WIDTH

/**
 * Column widths saved from a previous visit, or none.
 *
 * Anything partial or malformed is discarded rather than merged: a half-filled
 * record would leave some columns on a stored pixel width and the rest on a
 * default, which adds up to a table wider than the page for no reason the
 * reader could see. Empty means "share the space", which always fits.
 */
function readWidths(): Record<string, number> {
  try {
    const saved = localStorage.getItem(WIDTHS_KEY)
    if (!saved) return {}
    const parsed: unknown = JSON.parse(saved)
    if (!parsed || typeof parsed !== "object") return {}
    const record = parsed as Record<string, unknown>
    const out: Record<string, number> = {}
    for (const column of COLUMNS) {
      const value = record[column.key]
      if (typeof value !== "number" || !Number.isFinite(value)) return {}
      if (value < MIN_COLUMN_WIDTH) return {}
      out[column.key] = value
    }
    return out
  } catch {
    return {}
  }
}

function percent(width: number): string {
  return `${((width / TOTAL_DEFAULT_WIDTH) * 100).toFixed(4)}%`
}
/* Versioned. The stored value used to be "widths the table was laid out
   with"; it is now "widths a person chose", and the presence of a value is
   what switches the table from sharing the space to fixed pixels. An entry
   written by the old meaning would put the table in pixel mode — and into a
   scrollbar — without anyone having asked. */
const WIDTHS_KEY = "dashboards-column-widths.v3"

type Sort = { key: ColumnKey; dir: "asc" | "desc" }

/** Most recently run first — the same default the list has always opened on. */
const DEFAULT_SORT: Sort = { key: "lastRun", dir: "desc" }

function sortItems(
  items: SavedDashboardSummary[],
  sort: Sort
): SavedDashboardSummary[] {
  const column = COLUMNS.find((c) => c.key === sort.key)
  if (!column) return items
  const factor = sort.dir === "asc" ? 1 : -1
  return [...items].sort((a, b) => {
    const left = column.value(a)
    const right = column.value(b)
    if (typeof left === "number" && typeof right === "number") {
      return (left - right) * factor
    }
    return String(left).localeCompare(String(right)) * factor
  })
}

/** Date and time. The column heading already says which moment this is, so
 *  the cell carries the value and nothing else. */
function formatStamp(value: string | null | undefined, empty = "—"): string {
  if (!value) return empty
  // Stored in UTC and sent without a zone; shown in the viewer's own time.
  const at = parseUtcTimestamp(value)
  return Number.isNaN(at.getTime()) ? empty : at.toLocaleString()
}

/** Dashboards whose database could not be determined. Kept last so the real
 *  groups read first, and named so nothing looks like it went missing. */
const UNGROUPED = "Ungrouped"
const FILE_GROUP = "Dashboards created from files"

type Group = { name: string; items: SavedDashboardSummary[] }

/**
 * The list, folded into its data groups.
 *
 * The group comes from the server so the rule lives in one place — today it is
 * the database a dashboard was built from, and one bucket for uploads. Groups
 * are alphabetical, with uploads and ungrouped last: those two are not
 * databases and reading them among them is confusing.
 */
function groupDashboards(items: SavedDashboardSummary[]): Group[] {
  const byName = new Map<string, SavedDashboardSummary[]>()
  for (const item of items) {
    const name = item.data_group || UNGROUPED
    const bucket = byName.get(name)
    if (bucket) bucket.push(item)
    else byName.set(name, [item])
  }
  const rank = (name: string) =>
    name === UNGROUPED ? 2 : name === FILE_GROUP ? 1 : 0
  return [...byName.entries()]
    .map(([name, groupItems]) => ({ name, items: groupItems }))
    .sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name))
}

/**
 * One folder's dashboards.
 *
 * Sorting lives here rather than above the list because it is a question about
 * the rows in front of you — "which of THESE is oldest" — and a control at the
 * top of the page cannot answer it per folder. The sort itself is shared, so
 * every folder is ordered the same way and two open folders never disagree.
 */
function FolderTable({
  items,
  sort,
  onSort,
  widths,
  onResize,
  onSeedWidths,
  canDelete,
  onMove,
  onDelete,
}: {
  items: SavedDashboardSummary[]
  sort: Sort
  onSort: (key: ColumnKey) => void
  widths: Record<string, number>
  onResize: (key: ColumnKey, width: number) => void
  onSeedWidths: (widths: Record<string, number>) => void
  canDelete: boolean
  onMove: (d: SavedDashboardSummary) => void
  onDelete: (d: SavedDashboardSummary) => void
}) {
  const rows = React.useMemo(() => sortItems(items, sort), [items, sort])

  /* Nothing resized yet means nothing has asked for a specific width, so the
     columns share the space and the table cannot overflow. */
  const resized = Object.keys(widths).length > 0
  const totalWidth =
    COLUMNS.reduce(
      (total, column) => total + (widths[column.key] ?? column.width),
      0
    ) + ACTIONS_WIDTH

  const headRow = React.useRef<HTMLTableRowElement>(null)

  /** The widths the browser actually chose, so the first drag starts from what
   *  is on screen rather than from the proportions it was laid out with. */
  const measure = React.useCallback((): Record<string, number> => {
    const row = headRow.current
    const seeded: Record<string, number> = {}
    if (!row) return seeded
    COLUMNS.forEach((column, index) => {
      const cell = row.cells[index]
      if (cell) seeded[column.key] = Math.round(cell.getBoundingClientRect().width)
    })
    return seeded
  }, [])

  /* Drag state lives in a ref: a resize fires on every pointer move, and
     routing that through React state re-renders the whole folder each pixel. */
  const drag = React.useRef<{ key: ColumnKey; startX: number; startW: number } | null>(
    null
  )

  const onPointerMove = React.useCallback(
    (event: React.PointerEvent) => {
      const active = drag.current
      if (!active) return
      const next = Math.max(
        MIN_COLUMN_WIDTH,
        active.startW + (event.clientX - active.startX)
      )
      onResize(active.key, Math.round(next))
    },
    [onResize]
  )

  const endDrag = React.useCallback((event: React.PointerEvent) => {
    if (!drag.current) return
    drag.current = null
    event.currentTarget.releasePointerCapture?.(event.pointerId)
  }, [])

  return (
    <div className="overflow-x-auto border-t">
      <table
        className={cn("table-fixed text-sm", !resized && "w-full")}
        style={resized ? { width: totalWidth, minWidth: "100%" } : undefined}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <colgroup>
          {COLUMNS.map((column) => (
            <col
              key={column.key}
              style={{
                width: resized
                  ? widths[column.key] ?? column.width
                  : percent(column.width),
              }}
            />
          ))}
          <col
            style={{
              width: resized ? ACTIONS_WIDTH : percent(ACTIONS_WIDTH),
            }}
          />
        </colgroup>
        <thead className="bg-muted/40">
          <tr
            ref={headRow}
            className="text-xs tracking-wide text-muted-foreground uppercase"
          >
            {COLUMNS.map((column) => {
              const active = sort.key === column.key
              return (
                <th
                  key={column.key}
                  aria-sort={
                    active
                      ? sort.dir === "asc"
                        ? "ascending"
                        : "descending"
                      : "none"
                  }
                  className="relative p-0 font-medium"
                >
                  <button
                    type="button"
                    onClick={() => onSort(column.key)}
                    className={cn(
                      "flex w-full items-center gap-1 px-4 py-2 hover:text-foreground",
                      column.align === "right" && "justify-end",
                      active && "text-foreground"
                    )}
                  >
                    <span className="truncate">{column.label}</span>
                    {active ? (
                      <ChevronDownIcon
                        className={cn(
                          "size-3 shrink-0 transition-transform",
                          sort.dir === "asc" && "rotate-180"
                        )}
                      />
                    ) : null}
                  </button>
                  {/* Grab the edge to narrow a column. Sits over the border so
                      the target is the line the eye already reads as the
                      boundary. */}
                  <span
                    role="separator"
                    aria-orientation="vertical"
                    aria-label={`Resize ${column.label}`}
                    onPointerDown={(event) => {
                      event.preventDefault()
                      // Freeze every column at its rendered width before the
                      // first drag, or the others would snap as the table
                      // switches from proportions to pixels.
                      const seeded = resized ? widths : measure()
                      if (!resized) onSeedWidths(seeded)
                      drag.current = {
                        key: column.key,
                        startX: event.clientX,
                        startW: seeded[column.key] ?? column.width,
                      }
                      event.currentTarget.setPointerCapture?.(event.pointerId)
                    }}
                    className="absolute top-0 right-0 h-full w-1.5 cursor-col-resize select-none hover:bg-border"
                  />
                </th>
              )
            })}
            <th className="w-[72px]" />
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr
              key={d.report_id}
              className="group/row border-t hover:bg-muted/40"
            >
              <td className="truncate px-4 py-2.5 font-medium">
                <Link
                  to={`/reports/dashboards/${d.report_id}`}
                  className="hover:underline"
                >
                  {d.report_title}
                </Link>
              </td>
              <td className="truncate px-4 py-2.5 text-muted-foreground">
                {d.updated_by ?? "—"}
              </td>
              <td className="truncate px-4 py-2.5 text-muted-foreground">
                {formatStamp(d.updated_at)}
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums">
                {d.run_count}
              </td>
              <td className="truncate px-4 py-2.5 text-muted-foreground">
                {formatStamp(d.last_run_at, "Never")}
              </td>
              <td className="pr-3">
                <div className="flex items-center justify-end gap-1">
                  {d.source_type === "file" ? null : (
                    <button
                      type="button"
                      aria-label={`Move ${d.report_title} to another folder`}
                      title="Move to folder"
                      onClick={() => onMove(d)}
                      className="rounded p-1 text-muted-foreground opacity-0 hover:bg-muted hover:text-foreground group-hover/row:opacity-100 focus-visible:opacity-100"
                    >
                      <FolderInputIcon className="size-4" />
                    </button>
                  )}
                  {canDelete ? (
                    <DeleteButton
                      label={d.report_title}
                      onClick={() => onDelete(d)}
                      className="opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100"
                    />
                  ) : null}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** A folder, open or shut. */
function FolderShell({
  name,
  count,
  open,
  onToggle,
  children,
}: {
  name: string
  count: number
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="flex w-full items-center gap-2.5 px-4 py-3 text-left hover:bg-muted/40"
      >
        <ChevronRightIcon
          className={cn(
            "size-4 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90"
          )}
        />
        <FolderIcon className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 truncate font-medium">{name}</span>
        <span className="ml-auto shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs tabular-nums text-muted-foreground">
          {count}
        </span>
      </button>
      {open ? children : null}
    </div>
  )
}

export function DashboardsList() {
  const [dashboards, setDashboards] = React.useState<SavedDashboardSummary[]>(
    []
  )
  const [isLoading, setIsLoading] = React.useState(true)
  const [error, setError] = React.useState<string | null>(null)
  const [query, setQuery] = React.useState("")
  /* Collapsed by default: with seventy-odd dashboards a flat list is the
     problem, and every group open again is the same list with headings. */
  const [expanded, setExpanded] = React.useState<Set<string>>(new Set())

  /* While searching, folders start OPEN, so what the chevron records is the
     opposite: which ones you have shut. Kept apart from `expanded` so a search
     does not disturb the folders you had open before it, and stamped with the
     query it belongs to so a new search starts open again — without an effect
     to clear it. */
  const [shut, setShut] = React.useState<{ query: string; names: Set<string> }>(
    { query: "", names: new Set() }
  )
  const [sort, setSort] = React.useState<Sort>(DEFAULT_SORT)

  /* Clicking the column you are already sorted by flips the direction; any
     other column starts on the order that reads naturally for it — names
     A to Z, dates and counts biggest first. */
  const onSort = React.useCallback((key: ColumnKey) => {
    setSort((prev) =>
      prev.key === key
        ? { key, dir: prev.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "title" || key === "editor" ? "asc" : "desc" }
    )
  }, [])

  /* Column widths are a workspace habit, so they last beyond the visit. A stored width for a column that no longer exists is
     simply never read. */
  const [widths, setWidths] = React.useState<Record<string, number>>(readWidths)

  const storeWidths = React.useCallback(
    (next: Record<string, number>) => {
      try {
        localStorage.setItem(WIDTHS_KEY, JSON.stringify(next))
      } catch {
        // A browser refusing storage is not a reason to stop resizing.
      }
      return next
    },
    []
  )

  const onSeedWidths = React.useCallback(
    (seeded: Record<string, number>) => setWidths(storeWidths(seeded)),
    [storeWidths]
  )

  const onResize = React.useCallback(
    (key: ColumnKey, width: number) =>
      setWidths((prev) => storeWidths({ ...prev, [key]: width })),
    [storeWidths]
  )

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const list = await fetchSavedDashboards()
        if (!cancelled) setDashboards(list)
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load")
        }
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Hiding the control for non-admins is a courtesy, not the boundary — the
  // server refuses the DELETE regardless of what the client renders.
  const { user } = useAuth()
  const canDelete = Boolean(user?.is_admin)
  const del = useDeleteTarget<{ id: string; name: string }>(
    deleteDashboard,
    React.useCallback((id: string) => {
      setDashboards((current) => current.filter((d) => d.report_id !== id))
      toast.success("Dashboard deleted")
    }, [])
  )

  const visible = React.useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = q
      ? dashboards.filter((d) =>
          `${d.report_title} ${d.updated_by ?? ""}`.toLowerCase().includes(q)
        )
      : dashboards
    // Ordering happens per folder, in the table header.
    return filtered
  }, [dashboards, query])

  const groups = React.useMemo(() => groupDashboards(visible), [visible])

  /** The dashboard whose folder is being changed, if any. */
  const [moving, setMoving] = React.useState<SavedDashboardSummary | null>(null)

  /* Folders that already exist, offered as choices. A folder is only ever the
     set of dashboards in it, so this list IS the set of folders — there is no
     separate place one could be created and left empty. */
  const folderNames = React.useMemo(
    () =>
      groupDashboards(dashboards)
        .map((g) => g.name)
        .filter((name) => name !== FILE_GROUP),
    [dashboards]
  )

  /* A search is a request to see the matches, so a non-empty box opens every
     group that still has rows in it. Derived rather than pushed into state:
     the open set is a function of the query, and storing it would mean keeping
     two sources of truth in step. */
  const searching = query.trim().length > 0

  const isOpen = React.useCallback(
    (name: string) =>
      searching
        ? !(shut.query === query && shut.names.has(name))
        : expanded.has(name),
    [searching, query, shut, expanded]
  )

  const toggleGroup = React.useCallback(
    (name: string) => {
      if (!searching) {
        setExpanded((prev) => {
          const next = new Set(prev)
          if (next.has(name)) next.delete(name)
          else next.add(name)
          return next
        })
        return
      }
      setShut((prev) => {
        const names = new Set(prev.query === query ? prev.names : [])
        if (names.has(name)) names.delete(name)
        else names.add(name)
        return { query, names }
      })
    },
    [searching, query]
  )

  if (isLoading) {
    return (
      <div className="mx-auto w-full max-w-5xl space-y-3 p-6">
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    )
  }

  return (
    // AppShell puts overflow-hidden on the outlet container (chat manages its
    // own ScrollArea), so a full-height scroll wrapper is needed here.
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-5xl space-y-4 p-6">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-medium">
            <LayoutDashboardIcon {...accent("dashboards", "size-5")} />
            Dashboards
          </h1>
          <p className="text-sm text-muted-foreground">
            Saved dashboards run live against the database each time you open
            them.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <SearchIcon className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by title or editor…"
              className="pl-8"
            />
          </div>

        </div>

        {error ? (
          <p className="text-sm text-destructive">Failed to load: {error}</p>
        ) : visible.length === 0 ? (
          <p className="py-10 text-center text-sm text-muted-foreground">
            {dashboards.length === 0
              ? "No dashboards saved yet. Ask in chat to build one, then save it."
              : "No matches."}
          </p>
        ) : (
          <div className="space-y-3">
            {groups.map((group) => (
              <FolderShell
                key={group.name}
                name={group.name}
                count={group.items.length}
                open={isOpen(group.name)}
                onToggle={() => toggleGroup(group.name)}
              >
                <FolderTable
                    items={group.items}
                    sort={sort}
                    onSort={onSort}
                    widths={widths}
                    onResize={onResize}
                    onSeedWidths={onSeedWidths}
                    canDelete={canDelete}
                    onMove={setMoving}
                    onDelete={(d) =>
                      del.ask({ id: d.report_id, name: d.report_title })
                    }
                  />
              </FolderShell>
            ))}
          </div>
        )}
      </div>

      {moving ? (
      <MoveToFolderDialog
        key={moving.report_id}
        dashboard={moving}
        folders={folderNames}
        onClose={() => setMoving(null)}
        onMoved={async () => {
          setMoving(null)
          /* Refetched rather than patched locally: which folder a dashboard
             lands in after a reset is the server's rule, and duplicating it
             here would be a second copy to keep in step. Called from a click,
             not an effect. */
          try {
            setDashboards(await fetchSavedDashboards())
          } catch (err) {
            setError(err instanceof Error ? err.message : "Failed to reload")
          }
        }}
      />
      ) : null}

      <DeleteDialog
        open={del.target !== null}
        name={del.target?.name ?? ""}
        kind="dashboard"
        busy={del.busy}
        error={del.error}
        onConfirm={del.confirm}
        onOpenChange={(next) => {
          if (!next) del.close()
        }}
      />
    </div>
  )
}

/**
 * Move one dashboard into a folder.
 *
 * Offers the folders that exist and accepts a new name, because a folder IS
 * the dashboards filed under it — typing a name nothing uses yet is how one
 * gets created, and emptying the box is how a dashboard goes back to being
 * filed under its own database.
 */
function MoveToFolderDialog({
  dashboard,
  folders,
  onClose,
  onMoved,
}: {
  /* Never null: the parent keys this on the dashboard's id, so choosing a
     different one mounts a fresh dialog with its own initial state instead of
     an effect copying props into state after the fact. */
  dashboard: SavedDashboardSummary
  folders: string[]
  onClose: () => void
  onMoved: () => void | Promise<void>
}) {
  const [name, setName] = React.useState(dashboard.data_group_override ?? "")
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  const current = dashboard.data_group ?? ""
  const overridden = Boolean(dashboard.data_group_override)

  async function submit(value: string | null) {
    setBusy(true)
    setError(null)
    try {
      await setDashboardGroup(dashboard.report_id, value)
      toast.success(
        value ? `Moved to ${value}` : "Moved back to its database"
      )
      await onMoved()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not move it")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(next) => (next ? null : onClose())}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Move to folder</DialogTitle>
          <DialogDescription>
            {dashboard.report_title} is in {current || "no folder"}.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <Input
            autoFocus
            value={name}
            placeholder="Folder name"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && name.trim()) void submit(name.trim())
            }}
          />
          {folders.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {folders.map((folder) => (
                <button
                  key={folder}
                  type="button"
                  onClick={() => setName(folder)}
                  className={cn(
                    "rounded-full border px-2.5 py-1 text-xs hover:bg-muted",
                    name === folder && "border-foreground/40 bg-muted"
                  )}
                >
                  {folder}
                </button>
              ))}
            </div>
          ) : null}
          {error ? <p className="text-xs text-destructive">{error}</p> : null}
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          {overridden ? (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => void submit(null)}
            >
              Back to its database
            </Button>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button
              disabled={busy || !name.trim() || name.trim() === current}
              onClick={() => void submit(name.trim())}
            >
              Move
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Quiet until you look for it: a destructive control on every row of a list
 *  competes with the thing the list is actually for. */
function DeleteButton({
  label,
  onClick,
  className,
}: {
  label: string
  onClick: () => void
  className?: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`Delete ${label}`}
      title={`Delete ${label}`}
      className={cn(
        "rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
        className
      )}
    >
      <Trash2Icon className="size-4" />
    </button>
  )
}

export default DashboardsList
