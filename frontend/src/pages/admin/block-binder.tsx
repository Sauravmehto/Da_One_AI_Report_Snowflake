import * as React from "react"
import { toast } from "sonner"
import {
  AlertTriangleIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  DownloadIcon,
  Loader2Icon,
  PlayIcon,
} from "lucide-react"

import {
  SaveReportDialog,
  type SavePhase,
} from "@/components/admin/save-report-dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  fetchScanBlockPlan,
  formatApiError,
  multiBlockDownloadUrl,
  runMultiBlock,
  type BlockPlan,
  type BlockToBind,
  type MultiBlockConfig,
  type MultiBlockRun,
  type PlannedSheet,
} from "@/lib/excel-api"
import { cn } from "@/lib/utils"

/**
 * Configure a workbook's data areas, run it, and only then save it.
 *
 * Three things the first version got wrong, all of them structural:
 *
 *  - It asked you to approve the template BEFORE binding anything, so you
 *    committed before knowing whether the app had read the workbook correctly.
 *    Approval is now the last step, after you have seen a generated file.
 *  - It called everything a "block". Nobody has blocks; they have a sheet, and
 *    an area within it. Every location now reads `SHEET · rows 3–7`.
 *  - It showed what was detected but let you change none of it. A wrong guess
 *    was a dead end. Each sheet's role is now editable, and marking one "takes
 *    data" brings its areas back for binding.
 */

// "parameter" is a real answer, not an absence of one — a settings sheet gets
// no query and no data. Leaving it out of the union meant such a sheet fell
// back to the first option and displayed as "Takes data", which was a lie.
type SheetRole = "source" | "computed" | "parameter"

export function BlockBinder({
  scanId,
  reportName,
  onBack,
  onSave,
}: {
  scanId: string
  reportName: string
  onBack: () => void
  /** Persists the workbook AND this binding. Rejects on failure; the parent
   *  reports the error, this component only drives the dialog's phase. */
  onSave: (config: MultiBlockConfig) => Promise<void>
}) {
  const [plan, setPlan] = React.useState<BlockPlan | null>(null)
  const [loadError, setLoadError] = React.useState<string | null>(null)
  const [sql, setSql] = React.useState<Record<string, string>>({})
  const [overrides, setOverrides] = React.useState<Record<string, SheetRole>>({})
  const [areaOn, setAreaOn] = React.useState<Record<string, boolean>>({})
  const [openKey, setOpenKey] = React.useState<string | null>(null)
  const [asOfDate, setAsOfDate] = React.useState("")
  const [running, setRunning] = React.useState(false)
  const [runError, setRunError] = React.useState<string | null>(null)
  const [result, setResult] = React.useState<MultiBlockRun | null>(null)
  const [saveOpen, setSaveOpen] = React.useState(false)
  const [savePhase, setSavePhase] = React.useState<SavePhase>("confirm")

  // Re-planned whenever a role is corrected, so the dropdown actually changes
  // which areas are offered rather than only relabelling a row.
  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const p = await fetchScanBlockPlan(scanId, overrides, areaOn)
        if (!cancelled) setPlan(p)
      } catch (err) {
        if (!cancelled) setLoadError(formatApiError(err))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [scanId, overrides, areaOn])

  const areas = plan?.blocks_to_bind ?? []
  const bound = areas.filter((a) => sql[a.key]?.trim()).length
  const ready = areas.length > 0 && bound === areas.length
  const usesDate = areas.some((a) => sql[a.key]?.includes("{{as_of_date}}"))

  // One description of the binding, used both to generate and to save — so what
  // gets stored is exactly what produced the file you just checked, rather than
  // a second construction of the same idea that could drift from it.
  function config(): MultiBlockConfig {
    return {
      block_sql: Object.fromEntries(areas.map((a) => [a.key, sql[a.key]])),
      // Declared only when a query actually references it. A parameter nothing
      // uses still has to resolve at run time, so declaring it unconditionally
      // would make a report fail for want of a date it never reads.
      parameters: usesDate
        ? [
            {
              name: "as_of_date",
              type: "date",
              label: "Report date",
              ...(asOfDate ? { default: asOfDate } : {}),
            },
          ]
        : [],
      role_overrides: overrides,
      block_overrides: areaOn,
    }
  }

  async function handleRun() {
    setRunning(true)
    setRunError(null)
    try {
      const run = await runMultiBlock({
        scan_id: scanId,
        report_title: reportName.trim() || "Report",
        ...config(),
        parameter_values: asOfDate ? { as_of_date: asOfDate } : {},
      })
      setResult(run)
      toast.success("Report generated")
    } catch (err) {
      const message = formatApiError(err)
      setRunError(message)
      toast.error("Could not generate", { description: message })
    } finally {
      setRunning(false)
    }
  }

  async function handleSave() {
    setSavePhase("saving")
    try {
      await onSave(config())
      setSavePhase("done")
    } catch {
      // The parent has already surfaced the message; just release the dialog so
      // the binding is still there to correct.
      setSaveOpen(false)
      setSavePhase("confirm")
    }
  }

  if (loadError) return <p className="text-sm text-destructive">{loadError}</p>
  if (!plan) return <p className="text-sm text-muted-foreground">Reading the workbook…</p>

  return (
    <div className="space-y-8">
      <Button variant="ghost" size="sm" onClick={onBack} className="-ml-2 gap-1.5">
        <ChevronLeftIcon className="size-4" />
        Back to scan
      </Button>

      <Section
        n={1}
        title="Check what each sheet does"
        hint="Change any that we read wrongly."
      >
        <ul className="divide-y overflow-hidden rounded-lg border">
          {plan.sheets.map((s) => (
            <SheetRow
              key={s.sheet}
              sheet={s}
              value={(overrides[s.sheet] ?? s.role) as SheetRole}
              detected={s.role}
              onChange={(v) =>
                setOverrides((prev) => ({ ...prev, [s.sheet]: v }))
              }
            />
          ))}
        </ul>
        {plan.warnings.map((w, i) => (
          <p
            key={i}
            className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-400"
          >
            <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
            {w}
          </p>
        ))}
      </Section>

      <Section
        n={2}
        title="Write a query for each data area"
        hint={`${bound} of ${areas.length} done. Name each column exactly as the sheet's header — that is how a value finds its column.`}
      >
        {/* One editor at a time. Six always-open textareas made this section a
            wall you had to scroll past rather than a list you could read. */}
        <ul className="divide-y overflow-hidden rounded-lg border">
          {areas.map((area) => (
            <AreaCard
              key={area.key}
              area={area}
              open={openKey === area.key}
              onToggle={() =>
                setOpenKey((k) => (k === area.key ? null : area.key))
              }
              value={sql[area.key] ?? ""}
              onChange={(v) => setSql((prev) => ({ ...prev, [area.key]: v }))}
            />
          ))}
        </ul>
        {(plan.unbound_areas ?? []).length > 0 ? (
          <details className="group rounded-lg border px-4 py-2.5 text-xs">
            <summary className="flex cursor-pointer list-none items-center gap-1.5 text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
              <ChevronRightIcon className="size-3 transition-transform group-open:rotate-90" />
              {plan.unbound_areas.length} other tables we are not filling
            </summary>
            {/* Listed, not hidden. A table the app silently ignored looked like
                a table it had failed to see — and one of them really did want a
                query of its own. */}
            <ul className="mt-2 space-y-1.5">
              {plan.unbound_areas.map((a) => (
                <li key={a.key} className="flex items-center gap-2">
                  <span className="min-w-0 flex-1">
                    <span className="font-mono">{a.sheet}</span> rows{" "}
                    {a.first_row}–{a.last_row}
                    <span className="text-muted-foreground"> — {a.why}</span>
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      setAreaOn((prev) => ({ ...prev, [a.key]: true }))
                    }
                    className="shrink-0 rounded border px-2 py-0.5 hover:bg-muted"
                  >
                    Fill this too
                  </button>
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </Section>

      <Section
        n={3}
        title="Generate and check"
        hint="Nothing is saved yet. Open the file and confirm it looks right."
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="as_of" className="text-xs text-muted-foreground">
              Report date (optional)
            </Label>
            <Input
              id="as_of"
              type="date"
              value={asOfDate}
              onChange={(e) => setAsOfDate(e.target.value)}
              className="w-[170px]"
            />
          </div>
          <p className="flex-1 pb-1 text-xs text-muted-foreground">
            Only queries that mention{" "}
            <code className="font-mono">{"{{as_of_date}}"}</code> are filtered by
            it, and each one picks its own column — so one area can read{" "}
            <code className="font-mono">mark_date &lt;= {"{{as_of_date}}"}</code>{" "}
            while another takes full history.
          </p>
          <Button onClick={handleRun} disabled={running || !ready} className="gap-2">
            {running ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <PlayIcon className="size-4" />
            )}
            Generate
          </Button>
        </div>

        {runError ? (
          <p className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm whitespace-pre-wrap text-destructive">
            {runError}
          </p>
        ) : null}

        {result ? <RunSummary result={result} /> : null}
      </Section>

      <Section
        n={4}
        title="Save it as a report"
        hint="Keeps the workbook and these queries together, so it appears in Reports and can be re-run for another date."
      >
        <div className="flex flex-wrap items-center gap-3">
          <Button
            onClick={() => {
              setSavePhase("confirm")
              setSaveOpen(true)
            }}
            disabled={!result || savePhase === "saving"}
            className="gap-2"
          >
            {savePhase === "saving" ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : null}
            Save report
          </Button>
          {/* Also at the FOOT of the page. The one at the top is unreachable
              from here without scrolling past fourteen sheets, which is not a
              way out. */}
          <Button variant="ghost" onClick={onBack} className="gap-1.5">
            <ChevronLeftIcon className="size-4" />
            Back to scan
          </Button>
          {!result ? (
            <p className="text-xs text-muted-foreground">
              Save is available once you have generated a file.
            </p>
          ) : null}
        </div>
      </Section>

      <SaveReportDialog
        open={saveOpen}
        phase={savePhase}
        reportName={reportName.trim() || "Report"}
        areaCount={areas.length}
        parameterName={usesDate ? "report date" : null}
        onConfirm={handleSave}
        onOpenChange={setSaveOpen}
      />
    </div>
  )
}

function Section({
  n,
  title,
  hint,
  children,
}: {
  n: number
  title: string
  hint: string
  children: React.ReactNode
}) {
  return (
    <section className="space-y-3">
      <div className="flex items-baseline gap-2">
        <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-[11px] font-medium tabular-nums text-muted-foreground">
          {n}
        </span>
        <div className="min-w-0">
          <h2 className="text-sm font-medium">{title}</h2>
          <p className="text-xs text-muted-foreground">{hint}</p>
        </div>
      </div>
      <div className="space-y-2 pl-7">{children}</div>
    </section>
  )
}

const ROLE_TEXT: Record<string, { label: string; detail: string }> = {
  source: { label: "Takes data", detail: "we fill this from a query" },
  computed: {
    label: "Computed",
    detail: "the workbook calculates it — we only stretch its formulas",
  },
  parameter: { label: "Settings", detail: "left exactly as it is" },
}

function SheetRow({
  sheet,
  value,
  detected,
  onChange,
}: {
  sheet: PlannedSheet
  value: SheetRole
  detected: string
  onChange: (v: SheetRole) => void
}) {
  // Only two real answers. The earlier version offered "Takes data (detected)"
  // AND "Takes data" — the same outcome twice, because it leaked an internal
  // auto/explicit distinction into a menu. What was detected is now shown as a
  // note when you disagree with it, which is the only time it matters.
  const changed = value !== detected && detected !== "parameter"
  const text = ROLE_TEXT[value] ?? { label: value, detail: sheet.reason }

  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-2.5">
      <span className="min-w-0 flex-1">
        <span className="block font-mono text-xs font-medium">{sheet.sheet}</span>
        <span className="block text-xs text-muted-foreground">
          {changed ? `Changed by you — we read it as ${ROLE_TEXT[detected]?.label ?? detected}` : text.detail}
        </span>
      </span>
      {/* Parameter sheets get the SAME dropdown as everything else. Rendering
          them as static text made a wrong guess unfixable — a raw sheet read as
          "settings only" had no way back, which is the dead end this control
          exists to prevent. */}
      {(
        <select
          value={value}
          onChange={(e) => onChange(e.target.value as SheetRole)}
          aria-label={`What ${sheet.sheet} does`}
          className="h-8 shrink-0 rounded-md border bg-background px-2 text-xs"
        >
          <option value="source">Takes data</option>
          <option value="computed">Computed by the workbook</option>
          <option value="parameter">Settings only</option>
        </select>
      )}
    </li>
  )
}

function AreaCard({
  area,
  open,
  onToggle,
  value,
  onChange,
}: {
  area: BlockToBind
  open: boolean
  onToggle: () => void
  value: string
  onChange: (v: string) => void
}) {
  const done = Boolean(value.trim())
  const tight = area.spare_rows <= 2
  // Shown per area, because whether a block is date-filtered is a property of
  // its own query — not of the report.
  const usesDate = value.includes("{{as_of_date}}")

  return (
    <li>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-muted/50"
      >
        {done ? (
          <CheckIcon className="size-4 shrink-0 text-emerald-600" />
        ) : (
          <span className="size-4 shrink-0 rounded-full border border-dashed" />
        )}
        <span className="min-w-0 flex-1">
          {/* Location in the sheet's own terms. "Block" meant nothing to anyone. */}
          <span className="block truncate text-sm">
            <span className="font-mono font-medium">{area.sheet}</span>
            <span className="text-muted-foreground">
              {" "}rows {area.first_row}–{area.last_row}
            </span>
          </span>
          {area.title ? (
            <span className="block truncate text-xs text-muted-foreground">
              {area.title}
            </span>
          ) : null}
        </span>
        {usesDate ? (
          <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
            by date
          </span>
        ) : null}
        <span className="shrink-0 text-xs text-muted-foreground">
          {area.columns_needed.length} cols
        </span>
        <ChevronRightIcon
          className={cn(
            "size-4 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90"
          )}
        />
      </button>

      {open ? (
        <div className="space-y-3 border-t bg-muted/20 px-4 py-3">
          <Textarea
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder="SELECT …"
            className="min-h-32 bg-background font-mono text-xs"
            spellCheck={false}
            autoFocus
          />
          <div className="space-y-1.5 text-xs text-muted-foreground">
            <p>
              Name each column exactly as the sheet's header:
            </p>
            <div className="flex flex-wrap gap-1">
              {area.columns_needed.map((c) => (
                <code
                  key={c}
                  className="rounded border bg-background px-1.5 py-0.5 text-[11px]"
                >
                  {c}
                </code>
              ))}
            </div>
            {area.columns_computed_by_template.length > 0 ? (
              <p>
                Columns {area.columns_computed_by_template.join(", ")} are
                calculated by the sheet — leave them out.
              </p>
            ) : null}
            <p className={cn(tight && "text-amber-700 dark:text-amber-400")}>
              {area.spare_rows === 0
                ? "Another table starts immediately below — extra rows push it down, formulas and all."
                : `${area.spare_rows} empty rows below${tight ? " — more than that pushes the next table down, formulas and all." : "."}`}
            </p>
          </div>
        </div>
      ) : null}
    </li>
  )
}

function RunSummary({ result }: { result: MultiBlockRun }) {
  return (
    <div className="space-y-2 rounded-lg border bg-muted/20 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">{result.output_filename}</p>
        <Button variant="outline" size="sm" asChild className="gap-2">
          <a href={multiBlockDownloadUrl(result.download_id)} download>
            <DownloadIcon className="size-4" />
            Download
          </a>
        </Button>
      </div>
      <ul className="space-y-0.5 text-xs text-muted-foreground">
        {result.blocks_filled.map((b, i) => (
          <li key={i}>
            <span className="font-mono">{b.sheet}</span> rows {b.first_row}–
            {b.last_row} · {b.rows_written} rows written
            {b.filled_down.length
              ? ` · ${b.filled_down.length} calculated column(s) filled down`
              : ""}
          </li>
        ))}
        {result.blocks_regenerated.map((b, i) => (
          <li key={`r${i}`}>
            <span className="font-mono">{b.sheet}</span> row {b.first_row} ·
            grown to {b.rows} rows to match its source
          </li>
        ))}
      </ul>
    </div>
  )
}

export default BlockBinder
