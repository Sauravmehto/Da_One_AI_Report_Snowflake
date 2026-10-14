import * as React from "react"
import { toast } from "sonner"
import {
  ChevronRightIcon,
  DownloadIcon,
  FileSpreadsheetIcon,
  Loader2Icon,
  PlayIcon,
  Trash2Icon,
} from "lucide-react"
import { accent } from "@/lib/accents"

import { DeleteDialog } from "@/components/delete-dialog"
import { useDeleteTarget } from "@/hooks/use-delete-target"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { useAuth } from "@/contexts/auth-context"
import {
  deleteExcelReport,
  downloadUrl,
  fetchExcelReport,
  fetchExcelReports,
  formatApiError,
  runExcelReport,
  type ReportParameter,
  type SavedExcelReport,
} from "@/lib/excel-api"
import { cn } from "@/lib/utils"

/**
 * Saved Excel reports: pick a period, generate, download.
 *
 * Deliberately not the builder. Running a finished report is a monthly job with
 * no judgement in it — choose the period, press the button — so it gets a form.
 * Building one is a rare, careful activity and lives in the report builder.
 */
export function ReportsPage() {
  const [reports, setReports] = React.useState<SavedExcelReport[] | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [selected, setSelected] = React.useState<string | null>(null)

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const list = await fetchExcelReports()
        if (!cancelled) {
          setReports(list)
          setSelected((current) => current ?? list[0]?.report_id ?? null)
        }
      } catch (err) {
        if (!cancelled) {
          setError(formatApiError(err))
          setReports([])
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Shown to admins only. That is presentation; the server refuses the DELETE
  // for anyone else regardless of what the client chooses to render.
  const { user } = useAuth()
  const canDelete = Boolean(user?.is_admin)
  const del = useDeleteTarget<{ id: string; name: string }>(
    deleteExcelReport,
    React.useCallback((id: string) => {
      setReports((current) => (current ?? []).filter((r) => r.report_id !== id))
      setSelected((current) => (current === id ? null : current))
      toast.success("Report deleted")
    }, [])
  )

  if (reports === null) {
    return (
      <div className="mx-auto w-full max-w-4xl space-y-3 p-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }

  return (
    <div className="scrollbar-slim h-full min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-4xl space-y-6 p-6">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-medium">
            <FileSpreadsheetIcon {...accent("reports", "size-5")} />
            Reports
          </h1>
          <p className="text-sm text-muted-foreground">
            Excel reports generated from a saved template. Pick a period; the
            data sheet is refilled and the formulas recalculate when you open
            the workbook.
          </p>
        </div>

        {error ? (
          <p className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
            {error}
          </p>
        ) : null}

        {reports.length === 0 && !error ? (
          <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
            No reports yet. Build one from an Excel workbook in the report
            builder.
          </p>
        ) : null}

        <div className="space-y-3">
          {reports.map((report) => (
            <ReportCard
              key={report.report_id}
              report={report}
              expanded={selected === report.report_id}
              onToggle={() =>
                setSelected((current) =>
                  current === report.report_id ? null : report.report_id
                )
              }
              onDelete={
                canDelete
                  ? () =>
                      del.ask({
                        id: report.report_id,
                        name: report.report_title,
                      })
                  : undefined
              }
            />
          ))}
        </div>
      </div>

      <DeleteDialog
        open={del.target !== null}
        name={del.target?.name ?? ""}
        kind="report"
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

function ReportCard({
  report,
  expanded,
  onToggle,
  onDelete,
}: {
  report: SavedExcelReport
  expanded: boolean
  onToggle: () => void
  /** Omitted for non-admins, so the control is absent rather than disabled —
   *  a greyed-out button invites a question nobody here can answer. */
  onDelete?: () => void
}) {
  // The list endpoint returns metadata only — no parameters — so the card
  // fetched nothing and every report claimed to take none. A report whose query
  // needs a date then failed on Generate with no field to supply it. Details
  // are loaded when the card opens, which is the first moment they are needed.
  const [detail, setDetail] = React.useState<SavedExcelReport | null>(null)
  React.useEffect(() => {
    if (!expanded || detail) return
    let cancelled = false
    void fetchExcelReport(report.report_id)
      .then((full) => {
        if (!cancelled) setDetail(full)
      })
      .catch(() => {
        if (!cancelled) setDetail(report)
      })
    return () => {
      cancelled = true
    }
  }, [expanded, detail, report])

  const parameters = React.useMemo(
    () => detail?.parameters ?? [],
    [detail?.parameters]
  )
  // Only what the user has typed. Defaults are read from the parameter itself
  // at render, so the fields fill in as soon as the details arrive without a
  // second effect copying one piece of state into another.
  const [edited, setEdited] = React.useState<Record<string, string>>({})
  const valueOf = React.useCallback(
    (p: ReportParameter) =>
      edited[p.name] ?? (p.default != null ? String(p.default) : ""),
    [edited]
  )

  const [running, setRunning] = React.useState(false)
  const [lastRun, setLastRun] = React.useState<{
    rows?: number
    filename?: string
    truncated?: boolean
  } | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  async function handleRun() {
    setRunning(true)
    setError(null)
    try {
      const result = await runExcelReport(
        report.report_id,
        coerceValues(parameters, valueOf)
      )
      setLastRun({
        rows: result.row_count,
        filename: result.output_filename,
        truncated: Boolean(result.truncated),
      })
      toast.success("Report generated", {
        description: `${result.row_count ?? 0} rows written.`,
      })
      // Re-read the detail so the file just produced joins the run history —
      // otherwise it only appears after closing and reopening the card.
      void fetchExcelReport(report.report_id)
        .then(setDetail)
        .catch(() => {})
    } catch (err) {
      const message = formatApiError(err)
      setError(message)
      toast.error("Could not generate the report", { description: message })
    } finally {
      setRunning(false)
    }
  }

  // The newest already has its own Download button, so the list is what came
  // before it.
  const shown = detail?.runs ?? []
  const earlier = shown.slice(1)
  const hidden = Math.max(
    0,
    (detail?.runs_total ?? shown.length) - shown.length
  )
  const hasOutput =
    Boolean(lastRun) || Boolean(report.run_count) || shown.length > 0

  return (
    <div className="group/report overflow-hidden rounded-lg border">
      <div className="flex items-center hover:bg-muted/50">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-3 px-4 py-3 text-left"
        >
          <FileSpreadsheetIcon {...accent("reports")} />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium">
              {report.report_title}
            </span>
            <span className="block truncate text-xs text-muted-foreground">
              {report.template_name ?? "—"}
              {report.run_count ? ` · ${report.run_count} runs` : ""}
              {report.last_run_at
                ? ` · last ${new Date(report.last_run_at).toLocaleDateString()}`
                : ""}
            </span>
          </span>
        </button>
        {onDelete ? (
          <button
            type="button"
            onClick={onDelete}
            aria-label={`Delete ${report.report_title}`}
            title={`Delete ${report.report_title}`}
            className="mr-3 rounded-md p-1.5 text-muted-foreground opacity-0 transition-colors group-hover/report:opacity-100 hover:bg-destructive/10 hover:text-destructive focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            <Trash2Icon className="size-4" />
          </button>
        ) : null}
      </div>

      {expanded ? (
        <div className="space-y-4 border-t bg-muted/20 px-4 py-4">
          {detail === null ? (
            <Skeleton className="h-9 w-full" />
          ) : parameters.length > 0 ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {parameters.map((p) => (
                <div key={p.name} className="space-y-1">
                  <label className="text-xs font-medium">
                    {p.label ?? p.name}
                    <span className="ml-1.5 text-muted-foreground">
                      {p.type}
                    </span>
                  </label>
                  <Input
                    type={
                      p.type === "date"
                        ? "date"
                        : p.type === "number"
                          ? "number"
                          : "text"
                    }
                    value={valueOf(p)}
                    onChange={(e) =>
                      setEdited((v) => ({ ...v, [p.name]: e.target.value }))
                    }
                    className="h-9"
                  />
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              This report takes no parameters.
            </p>
          )}

          {/* What Generate is about to refill, in the sheet's own terms. A
              multi-block report touches several areas across several sheets,
              and nothing else on this page says which. */}
          {detail?.areas?.length ? (
            <p className="text-xs text-muted-foreground">
              Refills {detail.areas.length}{" "}
              {detail.areas.length === 1 ? "area" : "areas"}:{" "}
              {detail.areas.map((a) => a.replace("!", " row ")).join(", ")}. The
              rest of the workbook keeps its formulas.
            </p>
          ) : null}

          {error ? <p className="text-xs text-destructive">{error}</p> : null}

          {lastRun ? (
            <p
              className={cn(
                "text-xs",
                lastRun.truncated
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-muted-foreground"
              )}
            >
              {lastRun.rows ?? 0} rows written to {lastRun.filename}
              {lastRun.truncated
                ? " — the row cap was hit, so this extract is incomplete."
                : ""}
            </p>
          ) : null}

          <div className="flex items-center gap-2">
            <Button size="sm" onClick={handleRun} disabled={running || !detail}>
              {running ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                <PlayIcon className="size-4" />
              )}
              Generate
            </Button>
            {hasOutput ? (
              <Button size="sm" variant="outline" asChild>
                <a href={downloadUrl(report.report_id)} download>
                  <DownloadIcon className="size-4" />
                  Download
                </a>
              </Button>
            ) : null}
          </div>

          {/* Every run is kept, because the data underneath moves: re-running
              last quarter today gives different numbers for the same date, so
              the stored file is the only record of what was sent. Collapsed by
              default — the newest is what people usually want, and it already
              has a button. */}
          {earlier.length > 0 ? (
            <details className="group rounded-lg border bg-background px-3 py-2 text-xs">
              <summary className="flex cursor-pointer list-none items-center gap-1.5 text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
                <ChevronRightIcon className="size-3 transition-transform group-open:rotate-90" />
                {earlier.length} earlier {earlier.length === 1 ? "run" : "runs"}
              </summary>
              <ul className="mt-2 space-y-1">
                {earlier.map((r) => (
                  <li key={r.filename} className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate">
                      {r.generated_at
                        ? new Date(r.generated_at).toLocaleString()
                        : r.filename}
                    </span>
                    <a
                      href={downloadUrl(report.report_id, r.filename)}
                      download
                      className="inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 hover:bg-muted"
                    >
                      <DownloadIcon className="size-3" />
                      Download
                    </a>
                  </li>
                ))}
              </ul>
              {/* Said plainly rather than left implied. The older files still
                  exist and are still downloadable — this list is just short. */}
              {hidden > 0 ? (
                <p className="mt-2 text-muted-foreground">
                  {hidden} older {hidden === 1 ? "run is" : "runs are"} kept but
                  not listed here.
                </p>
              ) : null}
            </details>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/** Form inputs are strings; the query expects the declared type. */
function coerceValues(
  parameters: ReportParameter[],
  valueOf: (p: ReportParameter) => string
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const p of parameters) {
    const raw = valueOf(p)
    if (raw === "") continue
    out[p.name] = p.type === "number" ? Number(raw) : raw
  }
  return out
}

export default ReportsPage
