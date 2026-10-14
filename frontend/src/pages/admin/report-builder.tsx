import * as React from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { toast } from "sonner"
import {
  CheckIcon,
  FileSpreadsheetIcon,
  Loader2Icon,
  TriangleAlertIcon,
  UploadIcon,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import {
  fetchExcelReports,
  formatApiError,
  publishMultiBlock,
  scanWorkbook,
  type MultiBlockConfig,
  type ScanResult,
  type SavedExcelReport,
} from "@/lib/excel-api"
import { cn } from "@/lib/utils"
import { BlockBinder } from "@/pages/admin/block-binder"

/**
 * Excel report builder: upload → review → configure and run → save.
 *
 * Approval used to sit at step two, which asked you to commit a template before
 * you had configured anything or seen it produce a file. It is now last, and
 * the whole of configuration runs against the cached upload — so a workbook we
 * read wrongly is abandoned rather than stored.
 */

type Step = "upload" | "review" | "bind"

export function ReportBuilderPage() {
  const [step, setStep] = React.useState<Step>("upload")
  const [scan, setScan] = React.useState<ScanResult | null>(null)
  const [reportName, setReportName] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [existing, setExisting] = React.useState<SavedExcelReport[] | null>(
    null
  )

  React.useEffect(() => {
    void fetchExcelReports()
      .then(setExisting)
      .catch(() => setExisting([]))
  }, [])

  const reset = React.useCallback(() => {
    setScan(null)
    setReportName("")
    setError(null)
    setStep("upload")
  }, [])

  // Cleared on unmount, so leaving by hand during the success beat does not
  // fire a redirect underneath wherever the user went instead.
  const navigate = useNavigate()
  const redirectTimer = React.useRef<number | undefined>(undefined)
  React.useEffect(() => () => window.clearTimeout(redirectTimer.current), [])

  // Clicking "Report Builder" while already on it produces a new location key
  // but no remount, so the page kept whatever step it was on and the link
  // looked broken. Reset here instead — asking first, since a half-configured
  // workbook is real work to lose.
  const location = useLocation()
  const firstKey = React.useRef(location.key)
  const stepRef = React.useRef(step)
  React.useEffect(() => {
    stepRef.current = step
  })
  React.useEffect(() => {
    if (location.key === firstKey.current) return
    if (
      stepRef.current === "upload" ||
      window.confirm("Discard this workbook and start again?")
    ) {
      reset()
    }
  }, [location.key, reset])

  async function handleFile(file: File) {
    setBusy(true)
    setError(null)
    try {
      const result = await scanWorkbook(file)
      setScan(result)
      setReportName(result.template_name ?? "")
      setStep("review")
      toast.success("Workbook scanned", {
        description: `${result.summary?.sheets ?? 0} sheets classified.`,
      })
    } catch (err) {
      const message = formatApiError(err)
      setError(message)
      toast.error("Could not read that workbook", { description: message })
    } finally {
      setBusy(false)
    }
  }

  /**
   * Store the workbook AND the binding, in one call.
   *
   * This used to approve the template only, which kept the file and threw the
   * queries away — so the Reports section listed nothing and next month's copy
   * had to be configured from scratch. Rethrows so the binder can keep its
   * dialog in step; the message is reported here.
   */
  async function handleSave(config: MultiBlockConfig) {
    if (!scan) return
    setError(null)
    try {
      const saved = await publishMultiBlock(scan.scan_id, {
        ...config,
        report_title: reportName.trim() || scan.template_name || "Report",
      })
      toast.success("Report saved", {
        description: `${saved.report_title} is now in the Reports section.`,
      })
      // Hold the success tick, then go where the report actually lives. The
      // builder has nothing left to offer once a report is saved — its scan is
      // spent and its wizard is on the last step — and the dialog has just
      // promised the report is in Reports, so that is where to land.
      // 2200ms matches the semantic-layer publish, for one rhythm across both.
      redirectTimer.current = window.setTimeout(
        () => navigate("/reports"),
        2200
      )
    } catch (err) {
      const message = formatApiError(err)
      setError(message)
      toast.error("Could not save the report", { description: message })
      throw err
    }
  }

  return (
    <div className="scrollbar-slim h-full min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-4xl space-y-6 p-6">
        <div>
          <h1 className="text-xl font-medium">Report builder</h1>
          <p className="text-sm text-muted-foreground">
            Turn an Excel workbook into a report that regenerates itself for any
            period. The data sheet is refilled from a query; the formula sheets
            keep their formulas and re-extend to the new row count.
          </p>
        </div>

        <Steps current={step} />

        {error ? (
          <p className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
            {error}
          </p>
        ) : null}

        {step !== "upload" ? (
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="flex flex-col gap-1.5">
              <Label
                htmlFor="report-name"
                className="text-xs text-muted-foreground"
              >
                Report name
              </Label>
              <Input
                id="report-name"
                value={reportName}
                onChange={(e) => setReportName(e.target.value)}
                placeholder="Meridian Loan Extract"
                className="h-9 w-[280px]"
              />
            </div>
            <Button variant="ghost" size="sm" onClick={reset}>
              Use a different file
            </Button>
          </div>
        ) : null}

        {step === "upload" ? (
          <UploadStep busy={busy} onFile={handleFile} existing={existing} />
        ) : null}

        {step === "review" && scan ? (
          <ReviewStep
            scan={scan}
            busy={busy}
            onContinue={() => setStep("bind")}
            onStartOver={() => {
              setScan(null)
              setStep("upload")
            }}
          />
        ) : null}

        {/* The multi-block binder. The single-sheet BindStep it replaced could
            bind one sheet to one query, which no real template fits: this one
            needs three queries across two sheets, and its other two sheets must
            receive no data at all because the workbook computes them. */}
        {step === "bind" && scan ? (
          <BlockBinder
            scanId={scan.scan_id}
            reportName={reportName}
            onBack={() => setStep("review")}
            onSave={handleSave}
          />
        ) : null}
      </div>
    </div>
  )
}

function Steps({ current }: { current: Step }) {
  const steps: { key: Step; label: string }[] = [
    { key: "upload", label: "Upload" },
    { key: "review", label: "Review scan" },
    { key: "bind", label: "Configure & run" },
  ]
  const index = steps.findIndex((s) => s.key === current)

  return (
    <ol className="flex items-center gap-2 text-sm">
      {steps.map((s, i) => (
        <React.Fragment key={s.key}>
          {i > 0 ? <span className="h-px w-6 bg-border" aria-hidden /> : null}
          <li
            className={cn(
              "flex items-center gap-2 rounded-full px-3 py-1",
              i === index && "bg-accent font-medium text-accent-foreground",
              i < index && "text-muted-foreground"
            )}
          >
            {i < index ? (
              <CheckIcon className="size-3.5 text-emerald-600" />
            ) : (
              <span className="text-xs text-muted-foreground tabular-nums">
                {i + 1}
              </span>
            )}
            {s.label}
          </li>
        </React.Fragment>
      ))}
    </ol>
  )
}

function UploadStep({
  busy,
  onFile,
  existing,
}: {
  busy: boolean
  onFile: (file: File) => void
  existing: SavedExcelReport[] | null
}) {
  const inputRef = React.useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = React.useState(false)

  return (
    <div className="space-y-6">
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          const file = e.dataTransfer.files?.[0]
          if (file) onFile(file)
        }}
        className={cn(
          "flex flex-col items-center gap-3 rounded-xl border-2 border-dashed p-10 text-center transition-colors",
          dragging ? "border-primary bg-accent/40" : "border-border"
        )}
      >
        {busy ? (
          <Loader2Icon className="size-8 animate-spin text-muted-foreground" />
        ) : (
          <FileSpreadsheetIcon className="size-8 text-muted-foreground" />
        )}
        <div>
          <p className="text-sm font-medium">
            {busy ? "Scanning the workbook…" : "Drop an .xlsx here"}
          </p>
          <p className="text-xs text-muted-foreground">
            Nothing is saved until you have generated a report and kept it.
          </p>
        </div>
        <input
          ref={inputRef}
          type="file"
          accept=".xlsx,.xlsm"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) onFile(file)
            e.target.value = ""
          }}
        />
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
        >
          <UploadIcon className="size-4" />
          Choose a file
        </Button>
      </div>

      {existing === null ? (
        <Skeleton className="h-20 w-full" />
      ) : existing.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-medium">
            Already built
            <span className="ml-2 text-muted-foreground">
              {existing.length}
            </span>
          </h2>
          <ul className="divide-y overflow-hidden rounded-lg border">
            {existing.map((r) => (
              <li
                key={r.report_id}
                className="flex items-center gap-3 px-4 py-2.5 text-sm"
              >
                <FileSpreadsheetIcon className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">
                  {r.report_title}
                </span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {r.template_name ?? "—"}
                </span>
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground">
            Run these from the Reports section.
          </p>
        </section>
      ) : null}
    </div>
  )
}

function ReviewStep({
  scan,
  busy,
  onContinue,
  onStartOver,
}: {
  scan: ScanResult
  busy: boolean
  onContinue: () => void
  onStartOver: () => void
}) {
  const s = scan.summary ?? ({} as ScanResult["summary"])
  const needsReview = s.tier_2_needs_review ?? 0
  const tier2 = (scan.formula_classification?.tier_2 ?? []).filter(
    (f) => f.needs_review
  )
  const dataSheets = scan.sheets.filter((sheet) => sheet.role === "data")

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        {/* No role badge here any more. This screen used the OLD sheet-level
            scanner while the next uses block detection, so the same workbook
            was labelled two different ways on two consecutive screens. The
            authoritative answer — and the only editable one — is next. */}
        <h2 className="text-sm font-medium">Sheets</h2>
        <ul className="divide-y overflow-hidden rounded-lg border">
          {scan.sheets.map((sheet) => (
            <li
              key={sheet.name}
              className="flex items-center gap-3 px-4 py-2.5"
            >
              <span className="min-w-0 flex-1 truncate font-mono text-sm">
                {sheet.name}
              </span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {sheet.formula_count} formulas · {sheet.value_count} values
              </span>
            </li>
          ))}
        </ul>
        {dataSheets.length === 0 ? (
          <p className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
            <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
            No data sheet was identified, so there is nowhere to write query
            results. This workbook cannot be used as a report template.
          </p>
        ) : null}
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-medium">Formulas</h2>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat
            label="Auto-extend"
            value={s.tier_1 ?? 0}
            hint="aggregates over ranges"
          />
          <Stat
            label="Fill-down blocks"
            value={s.tier_1b_blocks ?? 0}
            hint={`${s.tier_1b_cells ?? 0} cells`}
          />
          <Stat
            label="Left as-is"
            value={s.tier_3 ?? 0}
            hint="self-contained"
          />
          <Stat
            label="Needs review"
            value={needsReview}
            hint="never rewritten"
            tone={needsReview > 0 ? "warn" : "ok"}
          />
        </dl>
      </section>

      {tier2.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-medium">
            Formulas we will not touch
            <span className="ml-2 text-muted-foreground">{tier2.length}</span>
          </h2>
          <p className="text-xs text-muted-foreground">
            These could not be proven safe to regenerate, so they are left
            exactly as written. If the data-row count changes, check they still
            point where you intend.
          </p>
          <ul className="divide-y overflow-hidden rounded-lg border">
            {tier2.slice(0, 12).map((f, i) => (
              <li key={`${f.sheet}-${f.cell}-${i}`} className="px-4 py-2.5">
                <p className="font-mono text-xs">
                  <span className="text-muted-foreground">
                    {f.sheet}!{f.cell}
                  </span>{" "}
                  {f.formula}
                </p>
                {f.reason ? (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {f.reason}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
          {tier2.length > 12 ? (
            <p className="text-xs text-muted-foreground">
              …and {tier2.length - 12} more.
            </p>
          ) : null}
        </section>
      ) : null}

      <div className="flex items-center gap-2">
        {/* Continue, not "approve": nothing is committed until a report has
            actually been generated and looked at. */}
        <Button onClick={onContinue} disabled={busy}>
          Continue
        </Button>
        <Button variant="ghost" onClick={onStartOver} disabled={busy}>
          Back
        </Button>
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  hint,
  tone = "ok",
}: {
  label: string
  value: number
  hint?: string
  tone?: "ok" | "warn"
}) {
  return (
    <div
      className={cn(
        "rounded-lg border p-3",
        tone === "warn" && value > 0 && "border-amber-500/40 bg-amber-500/5"
      )}
    >
      <dt className="text-[11px] tracking-wide text-muted-foreground uppercase">
        {label}
      </dt>
      <dd className="mt-0.5 text-lg font-medium tabular-nums">{value}</dd>
      {hint ? (
        <p className="text-[11px] text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  )
}

export default ReportBuilderPage
