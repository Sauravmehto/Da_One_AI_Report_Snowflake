import * as React from "react"
import { Link, useParams } from "react-router-dom"
import { toast } from "sonner"
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  ChevronRightIcon,
  CircleIcon,
  SlidersHorizontalIcon,
  TableIcon,
} from "lucide-react"

import { TableEditor } from "@/components/admin/table-editor"
import {
  PublishDialog,
  type PublishPhase,
} from "@/components/admin/publish-dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchTables, type TableSummary } from "@/lib/api"
import {
  discardDraft,
  emptyDocument,
  fetchLayer,
  publishLayer,
  PublishRejectedError,
  saveDraft,
  type Issue,
  type LayerDetail,
  type SemanticDocument,
  type TableDoc,
} from "@/lib/semantic-api"
import { cn } from "@/lib/utils"

const EMPTY_TABLE: TableDoc = {
  description: "",
  notes: "",
  key_columns: {},
  date_roles: {},
}

type Selection = { kind: "overview" } | { kind: "table"; fullName: string }

export function SemanticEditorPage() {
  const { database = "" } = useParams()

  const [layer, setLayer] = React.useState<LayerDetail | null>(null)
  const [doc, setDoc] = React.useState<SemanticDocument | null>(null)
  const [tables, setTables] = React.useState<TableSummary[]>([])
  const [issues, setIssues] = React.useState<Issue[]>([])
  const [selection, setSelection] = React.useState<Selection>({ kind: "overview" })

  const [isLoading, setIsLoading] = React.useState(true)
  const [isSaving, setIsSaving] = React.useState(false)
  const [isDirty, setIsDirty] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [tablesOpen, setTablesOpen] = React.useState(true)

  const [publishOpen, setPublishOpen] = React.useState(false)
  const [publishPhase, setPublishPhase] = React.useState<PublishPhase>("confirm")

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const [detail, tableList] = await Promise.all([
          fetchLayer(database),
          fetchTables(database).catch(() => [] as TableSummary[]),
        ])
        if (cancelled) return
        setLayer(detail)
        setDoc(detail.document ?? emptyDocument(database))
        setIssues(detail.issues)
        setTables(tableList)
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
  }, [database])

  function patchDoc(changes: Partial<SemanticDocument>) {
    setDoc((prev) => (prev ? { ...prev, ...changes } : prev))
    setIsDirty(true)
  }

  function patchTable(fullName: string, next: TableDoc) {
    setDoc((prev) =>
      prev ? { ...prev, tables: { ...prev.tables, [fullName]: next } } : prev
    )
    setIsDirty(true)
  }

  async function handleSave() {
    if (!doc) return
    setIsSaving(true)
    setError(null)
    try {
      const detail = await saveDraft(database, doc)
      setLayer(detail)
      setIssues(detail.issues)
      setIsDirty(false)
      toast.success("Draft saved successfully", {
        description: "Not live until you publish.",
      })
    } catch (err) {
      const message = err instanceof Error ? err.message : "Save failed"
      setError(message)
      toast.error("Could not save draft", { description: message })
    } finally {
      setIsSaving(false)
    }
  }

  async function handlePublish() {
    if (!doc) return
    setPublishPhase("publishing")
    setError(null)
    try {
      // Always save first: publishing promotes the stored draft, so publishing
      // with unsaved edits would quietly ship the previous version.
      await saveDraft(database, doc)
      const detail = await publishLayer(database)
      setLayer(detail)
      setDoc(detail.document ?? doc)
      setIssues(detail.issues)
      setIsDirty(false)
      setPublishPhase("done")
      // Hold the success state briefly, then close. Publishing changes nothing
      // visible on the page, so an instant dismissal reads as "did that work?"
      window.setTimeout(() => {
        setPublishOpen(false)
        setPublishPhase("confirm")
      }, 2200)
    } catch (err) {
      setPublishOpen(false)
      setPublishPhase("confirm")
      if (err instanceof PublishRejectedError) {
        setIssues(err.issues)
        setError("Cannot publish — fix the errors marked below.")
        toast.error("Publish blocked", {
          description: `${err.issues.filter((i) => i.severity === "error").length} error(s) must be fixed first.`,
        })
      } else {
        const message = err instanceof Error ? err.message : "Publish failed"
        setError(message)
        toast.error("Publish failed", { description: message })
      }
    }
  }

  async function handleDiscard() {
    setIsSaving(true)
    try {
      await discardDraft(database)
      const detail = await fetchLayer(database)
      setLayer(detail)
      setDoc(detail.document ?? emptyDocument(database))
      setIssues(detail.issues)
      setIsDirty(false)
      toast.success("Draft discarded", {
        description: "Reverted to the published layer.",
      })
    } catch (err) {
      const message = err instanceof Error ? err.message : "Discard failed"
      setError(message)
      toast.error("Could not discard draft", { description: message })
    } finally {
      setIsSaving(false)
    }
  }

  const errorCount = issues.filter((i) => i.severity === "error").length
  const warningCount = issues.filter((i) => i.severity === "warning").length

  // Counted from the live table list, not the document: a documented table
  // that no longer exists should not inflate the score.
  const documentedCount = tables.filter(
    (t) => Object.keys(doc?.tables[t.full_name]?.key_columns ?? {}).length > 0
  ).length

  if (isLoading) {
    return (
      <div className="space-y-3 p-6">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (!doc) {
    return (
      <div className="p-6">
        <p className="text-sm text-destructive">{error ?? "Not found."}</p>
        <Link to="/admin/semantic" className="text-sm underline">
          Back to all layers
        </Link>
      </div>
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="shrink-0 border-b px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <Link
            to="/admin/semantic"
            className="text-muted-foreground hover:text-foreground"
            aria-label="Back to all layers"
          >
            <ArrowLeftIcon className="size-4" />
          </Link>

          <div className="min-w-0">
            <h1 className="truncate text-sm font-medium">
              {doc.display_name || database}
            </h1>
            <p className="truncate font-mono text-xs text-muted-foreground">
              {database}
            </p>
          </div>

          {layer ? (
            <span
              className={cn(
                "rounded-full px-2 py-0.5 text-[11px] font-medium",
                layer.is_published
                  ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                  : "bg-muted text-muted-foreground"
              )}
            >
              {layer.is_published ? "Published" : "Not published"}
            </span>
          ) : null}
          {isDirty || layer?.has_draft ? (
            <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-400">
              {isDirty ? "Unsaved changes" : "Draft"}
            </span>
          ) : null}

          <div className="ml-auto flex items-center gap-2">
            {layer?.has_draft ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleDiscard}
                disabled={isSaving}
              >
                Discard draft
              </Button>
            ) : null}
            <Button
              variant="outline"
              size="sm"
              onClick={handleSave}
              disabled={isSaving || !isDirty}
            >
              Save draft
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setPublishPhase("confirm")
                setPublishOpen(true)
              }}
              disabled={isSaving}
            >
              Publish
            </Button>
          </div>
        </div>

        {layer ? (
          <div className="mt-2 flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
            <span>
              {layer.coverage.documented_tables}/{layer.coverage.total_tables}{" "}
              tables
            </span>
            <span>
              {layer.coverage.documented_columns}/{layer.coverage.total_columns}{" "}
              columns
            </span>
            <span>{layer.coverage.tables_with_date_roles} with date roles</span>
            {errorCount > 0 ? (
              <span className="flex items-center gap-1 text-destructive">
                <AlertTriangleIcon className="size-3.5" />
                {errorCount} error{errorCount === 1 ? "" : "s"}
              </span>
            ) : null}
            {warningCount > 0 ? (
              <span className="text-amber-600 dark:text-amber-400">
                {warningCount} warning{warningCount === 1 ? "" : "s"}
              </span>
            ) : null}
          </div>
        ) : null}

        {/* Transient confirmations are toasts; only errors persist here, since
            those need to stay visible while you go and fix them. */}
        {error ? (
          <p className="mt-2 text-xs text-destructive">{error}</p>
        ) : null}
      </div>

      {/* ── Body: schema on the left, document on the right ─────────────── */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <nav className="scrollbar-slim w-72 shrink-0 space-y-1 overflow-y-auto overscroll-contain border-r p-2">
          <button
            type="button"
            onClick={() => setSelection({ kind: "overview" })}
            className={cn(
              "flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm transition-colors",
              selection.kind === "overview"
                ? "bg-accent font-medium text-accent-foreground"
                : "hover:bg-muted/60"
            )}
          >
            <SlidersHorizontalIcon className="size-4 shrink-0 text-muted-foreground" />
            Overview &amp; rules
          </button>

          {/* Same shape as the item above, so the two read as siblings rather
              than as a button and a heading. */}
          <button
            type="button"
            onClick={() => setTablesOpen((open) => !open)}
            aria-expanded={tablesOpen}
            className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm transition-colors hover:bg-muted/60"
          >
            <TableIcon className="size-4 shrink-0 text-muted-foreground" />
            <span>Tables</span>
            <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground tabular-nums">
              {documentedCount}/{tables.length}
              <ChevronRightIcon
                className={cn(
                  "size-4 transition-transform duration-200",
                  tablesOpen && "rotate-90"
                )}
                aria-hidden
              />
            </span>
          </button>

          {tablesOpen ? (
            tables.length === 0 ? (
              <p className="px-3 py-2 text-xs text-muted-foreground">
                No tables readable in this database.
              </p>
            ) : (
              <ul className="ml-[1.4rem] space-y-0.5 border-l pl-2">
                {tables.map((table) => {
                  const tableDoc = doc.tables[table.full_name]
                  const described = Object.keys(
                    tableDoc?.key_columns ?? {}
                  ).length
                  const isSelected =
                    selection.kind === "table" &&
                    selection.fullName === table.full_name

                  return (
                    <li key={table.full_name}>
                      <button
                        type="button"
                        onClick={() =>
                          setSelection({
                            kind: "table",
                            fullName: table.full_name,
                          })
                        }
                        title={table.full_name}
                        className={cn(
                          "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left transition-colors",
                          isSelected
                            ? "bg-accent text-accent-foreground"
                            : "hover:bg-muted/60"
                        )}
                      >
                        <CircleIcon
                          className={cn(
                            "size-2 shrink-0",
                            !tableDoc || described === 0
                              ? "fill-muted-foreground/25 text-muted-foreground/25"
                              : tableDoc.description
                                ? "fill-emerald-500 text-emerald-500"
                                : "fill-amber-500 text-amber-500"
                          )}
                          aria-hidden
                        />
                        <span className="min-w-0 flex-1 truncate text-sm">
                          {table.name}
                        </span>
                        {described > 0 ? (
                          <span className="shrink-0 text-[11px] text-muted-foreground tabular-nums">
                            {described}
                          </span>
                        ) : null}
                      </button>
                    </li>
                  )
                })}
              </ul>
            )
          ) : null}
        </nav>

        <div className="scrollbar-slim min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl p-6">
            {selection.kind === "overview" ? (
              <OverviewEditor doc={doc} onChange={patchDoc} />
            ) : (
              <TableEditor
                key={selection.fullName}
                database={database}
                fullName={selection.fullName}
                doc={doc.tables[selection.fullName] ?? EMPTY_TABLE}
                issues={issues.filter((i) =>
                  i.field.includes(selection.fullName)
                )}
                onChange={(next) => patchTable(selection.fullName, next)}
              />
            )}
          </div>
        </div>
      </div>

      <PublishDialog
        open={publishOpen}
        phase={publishPhase}
        database={database}
        coverage={layer?.coverage ?? null}
        warningCount={warningCount}
        isFirstPublish={!layer?.is_published}
        onConfirm={handlePublish}
        onOpenChange={setPublishOpen}
      />
    </div>
  )
}

function OverviewEditor({
  doc,
  onChange,
}: {
  doc: SemanticDocument
  onChange: (changes: Partial<SemanticDocument>) => void
}) {
  return (
    <div className="space-y-6">
      <header className="flex items-center gap-2">
        <TableIcon className="size-4 text-muted-foreground" />
        <h2 className="text-sm font-medium">Overview &amp; rules</h2>
      </header>

      <div className="space-y-1.5">
        <label className="text-sm font-medium">Display name</label>
        <Input
          value={doc.display_name}
          onChange={(e) => onChange({ display_name: e.target.value })}
          placeholder="Private Credit Fund"
        />
      </div>

      <div className="space-y-1.5">
        <label className="text-sm font-medium">Description</label>
        <p className="text-xs text-muted-foreground">
          Shown in the database picker, and to the model as the opening line of
          its context.
        </p>
        <Textarea
          value={doc.description}
          onChange={(e) => onChange({ description: e.target.value })}
          rows={3}
        />
      </div>

      <div className="space-y-1.5">
        <label className="text-sm font-medium">Join rules</label>
        <p className="text-xs text-muted-foreground">
          The highest-value section. State which tables join on what — and
          especially which pairs must <em>never</em> be joined. This is what
          stops a plausible-looking but meaningless query.
        </p>
        <Textarea
          value={doc.join_rules}
          onChange={(e) => onChange({ join_rules: e.target.value })}
          rows={8}
          className="font-mono text-xs"
          placeholder={
            "- PC_CASHFLOW joins PRIVATE_CREDIT on SECURITY_ID.\n" +
            "- PRIVATE_CREDIT and PC_INVESTORS do NOT join directly."
          }
        />
      </div>

      <div className="space-y-1.5">
        <label className="text-sm font-medium">Dashboard heuristics</label>
        <p className="text-xs text-muted-foreground">
          Guidance for the dashboard planner: which panels a given kind of
          request should produce.
        </p>
        <Textarea
          value={doc.dashboard_heuristics}
          onChange={(e) => onChange({ dashboard_heuristics: e.target.value })}
          rows={6}
          className="font-mono text-xs"
        />
      </div>
    </div>
  )
}

export default SemanticEditorPage
