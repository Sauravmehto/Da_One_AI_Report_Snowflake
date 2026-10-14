import * as React from "react"
import { toast } from "sonner"
import {
  ChevronRightIcon,
  Loader2Icon,
  PlusIcon,
  SparklesIcon,
  Trash2Icon,
  TriangleAlertIcon,
  XIcon,
} from "lucide-react"

import { DashboardEditAssistant } from "@/components/dashboard/dashboard-edit-assistant"
import { ResizableDock } from "@/components/dashboard/resizable-dock"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import {
  fetchDashboardConfig,
  updateDashboard,
  type DashboardAssistResult,
  type DashboardDraft,
  type DashboardPanelConfig,
  type DashboardParameter,
} from "@/lib/api"
import {
  dataLabelsFor,
  DATA_LABELS_CHOICES,
  LABEL_POSITION_CHOICES,
  FUNNEL_PERCENT_CHOICES,
  LEGEND_CHOICES,
  MARKERS_CHOICES,
  panelSeriesShapes,
  REFERENCE_CHOICES,
  seriesLabelPosition,
  UNIT_CHOICES,
  type DataLabels,
  type LabelPosition,
  type FunnelPercent,
  type LegendPosition,
  type AxisReference,
  type Markers,
  type PanelFormat,
  type ReferenceKind,
  type SeriesFormat,
  type SeriesShape,
  type Unit,
} from "@/lib/chart-standard"
import {
  accentColor,
  PALETTES,
  PALETTE_OPTIONS,
  seriesColor,
  seriesLabel,
} from "@/lib/dashboard-format"
import type { ColumnFormat, ColumnFormats } from "@/lib/dashboard-format"
import { sqlAliases } from "@/lib/sql-aliases"
import { cn } from "@/lib/utils"

/**
 * Edit a dashboard's stored configuration in place.
 *
 * Sits beside the live panels rather than in a separate admin page, because the
 * whole reason to edit is to see what the change did — and a config screen you
 * have to leave to check your work is the slow loop this replaces.
 *
 * Deliberately a form over the stored plan, not a JSON box. The plan is what
 * the renderer trusts, and a stray comma in raw JSON is a broken dashboard for
 * everybody. Fields here can only produce shapes the server already accepts.
 *
 * Saving is not local: every panel is re-validated against Snowflake, and a
 * panel that will not compile rejects the whole save. That is why the error
 * surface below is a first-class part of this component rather than a toast —
 * a failed save usually means one panel's SQL needs another look.
 */

/**
 * Every type the panel dispatch can draw.
 *
 * This list is the ONLY way a person can change a panel's chart, so anything
 * missing here is unreachable by hand, and a type absent from it leaves the
 * select showing nothing at all on a panel that really is a map or a treemap.
 * `table` is not here: a table is the ABSENCE of a chart, offered as the empty
 * option below, which is also how the planner and the server represent it.
 *
 * Kept in step with the renderer by test_chart_vocabulary.py.
 */
const CHART_TYPES = [
  "bar",
  "stacked_bar",
  // Draws as a bar panel with a colour column splitting the value into one
  // series per distinct value. Listed separately rather than aliased to "bar"
  // because panels are already saved under this name: without it the Chart
  // type select has no matching option, shows blank, and the next change
  // rewrites the panel as whatever gets picked.
  "grouped_bar",
  "line",
  "area",
  "pie",
  "donut",
  "scatter",
  "bubble",
  "heatmap",
  "radar",
  "treemap",
  "funnel",
  "combo",
  "area_line",
  "geo_us_states",
  "geo_countries",
  "kpi",
]

/**
 * Which optional columns each chart actually reads, taken from the renderer.
 *
 * Shown per type rather than always: `size` means nothing to a line chart, and
 * a form offering every field for every chart teaches people that most of it is
 * noise. The reverse mattered more — heatmap and bubble were selectable while
 * the columns they need to render (`color`, `size`) had no input at all, so a
 * panel could be set to a chart it could never draw.
 *
 * `color` is deliberately absent for stacked_bar: BarPanel ignores it when
 * stacked, because the series come from the y list instead.
 */
const CHART_FIELDS: Record<string, ReadonlyArray<"color" | "size" | "orientation">> = {
  bar: ["color", "orientation"],
  // Same options as `bar`: it IS a bar panel with a colour column splitting the
  // value into one series per distinct value. It was missing entirely, so
  // grouped bars offered no direction and no series column.
  grouped_bar: ["color", "orientation"],
  // No colour column on purpose — a stacked bar takes its segments from several
  // value columns, and the renderer ignores `color` for this type.
  stacked_bar: ["orientation"],
  line: ["color"],
  area: ["color"],
  scatter: ["color", "size"],
  bubble: ["color", "size"],
  heatmap: ["color"],
}

/**
 * Chart types drawn against a value axis that can be lifted off zero.
 *
 * stacked_bar is absent deliberately: its segments only read as parts of a
 * whole because the stack starts at zero. pie, donut, treemap and heatmap
 * encode magnitude as area or shade and have no axis to lift.
 */
const FITTABLE_AXIS = new Set([
  "bar", "grouped_bar", "line", "area", "scatter", "bubble", "combo", "radar",
  "area_line",
])

/** Chart types that draw distinct coloured series, so a palette applies.
 *  Heatmaps and maps carry their own continuous scale instead. */
const PALETTED = new Set([
  "bar", "grouped_bar", "stacked_bar", "line", "area", "pie", "donut",
  "doughnut", "scatter", "bubble", "radar", "treemap", "funnel", "combo",
  "area_line",
])

const COLUMN_FORMAT_OPTIONS = [
  "currency",
  "percent",
  "number",
  "ratio",
  "text",
  "date",
] as const

/** Client-side row identity, so removing a panel does not renumber the rest. */
/** The draft as it was before one assistant change, for Undo. */
type DraftSnapshot = {
  title: string
  panels: DashboardPanelConfig[]
  parameters: DashboardParameter[]
  rowIds: string[]
  aiChanged: Set<string>
}

let nextRowId = 0
const newRowId = () => `panel-${(nextRowId += 1)}`

function emptyPanel(): DashboardPanelConfig {
  return {
    title: "New panel",
    chart_type: "bar",
    x: null,
    y: null,
    color: null,
    size: null,
    orientation: null,
    display: null,
    sql: "",
    // Declared explicitly: the server rebuilds panels from its own field list,
    // so anything omitted here is stored as null rather than left alone.
    section: null,
    span: null,
    row_span: null,
    column_formats: null,
    format: null,
  }
}


/**
 * What x and y ACTUALLY are, per chart type.
 *
 * They were always labelled "X column" and "Y column", which is wrong often
 * enough to mislead: a horizontal bar puts its categories on the visual Y axis,
 * a map's x is a place, a funnel's x is a stage. Worse, it invited the
 * conclusion that switching to horizontal ought to swap the two columns — it
 * must not. `BarPanel` works out which axis carries the value FROM THE DATA, so
 * the category stays the category and only the drawing direction changes.
 */
function axisLabels(chartType: string): { x: string; y: string } {
  switch (chartType) {
    case "scatter":
      return { x: "X column (numeric)", y: "Y column (numeric)" }
    case "bubble":
      return { x: "X column (numeric)", y: "Y column (numeric)" }
    case "heatmap":
      return { x: "Column category", y: "Value column" }
    case "funnel":
      return { x: "Stage column", y: "Value column" }
    case "radar":
      return { x: "Dimension column", y: "Value columns (comma separated)" }
    case "treemap":
      return { x: "Label column", y: "Value column" }
    case "geo_us_states":
      return { x: "State column", y: "Value column" }
    case "geo_countries":
      return { x: "Country column", y: "Value column" }
    case "combo":
      return { x: "Category column", y: "Value columns — the LAST one is the line" }
    case "area_line":
      return {
        x: "Period column",
        y: "Value columns in the SAME unit — the LAST one is the target line",
      }
    case "stacked_bar":
      return { x: "Category column", y: "Value columns (comma separated)" }
    default:
      return { x: "Category column", y: "Value column" }
  }
}

/** Chart types that read neither x nor y, so showing the fields only misleads. */
const IGNORES_AXES = new Set(["kpi", ""])

/** What each chart type is called in the editor. The stored value stays the key. */
const CHART_TYPE_NAMES: Record<string, string> = {
  bar: "Bar",
  stacked_bar: "Stacked bar",
  grouped_bar: "Grouped bar",
  line: "Line",
  area: "Area",
  pie: "Pie",
  donut: "Donut",
  scatter: "Scatter",
  bubble: "Bubble",
  heatmap: "Heatmap",
  radar: "Radar",
  treemap: "Treemap",
  funnel: "Funnel",
  combo: "Bars and line",
  area_line: "Area and target line",
  geo_us_states: "US states map",
  geo_countries: "Countries map",
  kpi: "KPI tiles",
}

function chartTypeName(chartType: string | null | undefined): string {
  if (!chartType) return "Table"
  return CHART_TYPE_NAMES[chartType] ?? chartType
}

/** Chart types with titled axes, a number unit and a legend. */
const AXIS_TITLED = new Set([
  "bar", "grouped_bar", "stacked_bar", "line", "area", "scatter", "bubble",
  "combo", "area_line",
])

/** Chart types that write values, or names, beside their bars, points or places. */
const LABELLED = new Set([
  "bar", "grouped_bar", "stacked_bar", "line", "area", "combo", "area_line",
  "scatter", "bubble", "geo_countries", "geo_us_states",
])

/** Chart types whose labels name the point rather than give its value. */
const NAMED_POINTS = new Set(["scatter", "bubble"])

/** Chart types with bars whose value can sit inside or outside their end. */
const BAR_POSITIONED = new Set(["bar", "grouped_bar", "combo"])

/** Chart types that draw a line whose points can be marked. */
const MARKED = new Set(["line", "area", "combo", "area_line"])

const LABEL_POSITION_NAMES: Record<LabelPosition, string> = {
  auto: "Auto",
  inside_end: "Inside end",
  outside_end: "Outside end",
}

const MARKER_NAMES: Record<Markers, string> = {
  auto: "Auto",
  show: "Show",
  hide: "Hide",
}

const UNIT_NAMES: Record<Unit, string> = {
  none: "Full number",
  K: "Thousands (K)",
  M: "Millions (M)",
  B: "Billions (B)",
}

const DATA_LABEL_NAMES: Record<DataLabels, string> = {
  none: "None",
  max: "Highest",
  min: "Lowest",
  extremes: "Highest and lowest",
  all: "All",
}

const FUNNEL_PERCENT_NAMES: Record<FunnelPercent, string> = {
  first: "Share of the top stage",
  previous: "Step from the stage above",
  none: "Hidden",
}

const LEGEND_NAMES: Record<LegendPosition, string> = {
  bottom: "Bottom",
  right: "Right",
  none: "Hidden",
}

function hasFormatSettings(chartType: string): boolean {
  return (
    AXIS_TITLED.has(chartType) ||
    FITTABLE_AXIS.has(chartType) ||
    LABELLED.has(chartType) ||
    PALETTED.has(chartType)
  )
}

/**
 * The format settings a chart type still reads, for a change of type.
 *
 * Same rule as the columns: a setting the new type ignores would be invisible,
 * and would reappear on switching back.
 */
function formatForType(
  chartType: string,
  format: PanelFormat | null | undefined,
  panel: Pick<DashboardPanelConfig, "y" | "color">
): PanelFormat | null {
  if (!format) return null
  const out: PanelFormat = {}
  if (AXIS_TITLED.has(chartType)) {
    if (format.x_title !== undefined) out.x_title = format.x_title
    if (format.y_title !== undefined) out.y_title = format.y_title
    if (format.unit !== undefined) out.unit = format.unit
    if (format.legend !== undefined) out.legend = format.legend
  }
  if (LABELLED.has(chartType) && format.data_labels !== undefined) {
    out.data_labels = format.data_labels
  }
  if (BAR_POSITIONED.has(chartType) && format.label_position !== undefined) {
    out.label_position = format.label_position
  }
  if (MARKED.has(chartType) && format.markers !== undefined) out.markers = format.markers
  if (chartType === "stacked_bar" && format.stack_totals !== undefined) {
    out.stack_totals = format.stack_totals
  }
  if (chartType === "funnel" && format.funnel_percent !== undefined) {
    out.funnel_percent = format.funnel_percent
  }
  if (NAMED_POINTS.has(chartType)) {
    for (const key of ["x_min", "x_max", "y_min", "y_max"] as const) {
      if (format[key] !== undefined) out[key] = format[key]
    }
    if (format.x_reference) out.x_reference = format.x_reference
    if (format.y_reference) out.y_reference = format.y_reference
    if (format.trend_line !== undefined) out.trend_line = format.trend_line
  }
  if (format.series && panelSeriesShapes(chartType, panel.y, panel.color).length > 0) {
    out.series = format.series
  }
  return Object.keys(out).length > 0 ? out : null
}

/**
 * A panel with its series settings trimmed to the series it still draws.
 *
 * A column renamed in the SQL leaves its settings behind, matching nothing;
 * saving is where they go.
 */
function withoutStaleSeries(panel: DashboardPanelConfig): DashboardPanelConfig {
  const series = panel.format?.series
  if (!series) return panel
  const drawn = new Set(panelSeriesShapes(panel.chart_type, panel.y, panel.color).map((s) => s.key))
  const kept = Object.fromEntries(Object.entries(series).filter(([key]) => drawn.has(key)))
  if (Object.keys(kept).length === Object.keys(series).length) return panel
  const format: PanelFormat = { ...panel.format }
  if (Object.keys(kept).length > 0) format.series = kept
  else delete format.series
  return { ...panel, format: Object.keys(format).length > 0 ? format : null }
}


export function DashboardEditor({
  reportId,
  onClose,
  onSaved,
}: {
  reportId: string
  onClose: () => void
  /** Called after a successful save so the page can re-run and show the
   *  result — the point of editing here rather than elsewhere. */
  onSaved: () => void | Promise<void>
}) {
  const [panels, setPanels] = React.useState<DashboardPanelConfig[] | null>(
    null
  )
  const [rowIds, setRowIds] = React.useState<string[]>([])
  const [title, setTitle] = React.useState("")
  const [openPanel, setOpenPanel] = React.useState<number | null>(0)
  const [loadError, setLoadError] = React.useState<string | null>(null)
  const [saveError, setSaveError] = React.useState<string | null>(null)
  const saveErrorRef = React.useRef<HTMLParagraphElement>(null)

  /* The message sits below every panel while Save lives in the footer, so a
     rejected save changed nothing near the button: it read as saved, and
     reopening showed the old dashboard. Bring it into view once it is on
     screen -- an effect, because a scroll requested from the catch block can
     run before React has rendered the message and find nothing to scroll to.
     The panels are left as they are: opening one here expanded it after the
     scroll and pushed the message straight back out of sight. */
  React.useEffect(() => {
    if (saveError) {
      saveErrorRef.current?.scrollIntoView({ block: "center", behavior: "smooth" })
    }
  }, [saveError])
  const [saving, setSaving] = React.useState(false)
  const [confirmDiscard, setConfirmDiscard] = React.useState(false)

  /* What was loaded, to tell an edited dashboard from an untouched one. State
     rather than a ref: it is read while rendering, to decide whether closing
     needs to ask first. */
  const [baseline, setBaseline] = React.useState("")

  /* The dashboard's runtime parameters. Not edited by the form, but the
     assistant can change them, so they travel with the draft. */
  const [parameters, setParameters] = React.useState<DashboardParameter[]>([])
  const [loadedParameters, setLoadedParameters] = React.useState("[]")

  /* The assistant. Closed by default: the form is the main tool, and the dock
     takes half its height. */
  const [assistantOpen, setAssistantOpen] = React.useState(false)
  const assistantConversation = React.useRef<string | null>(null)
  /* Drafts as they were before each assistant change, newest last, for Undo. */
  const [undoStack, setUndoStack] = React.useState<DraftSnapshot[]>([])
  /* Rows the assistant changed since the last save, by row id, so they can
     be marked. */
  const [aiChanged, setAiChanged] = React.useState<Set<string>>(new Set())
  /* What was asked of the assistant since the last save, for the version note. */
  const [aiRequests, setAiRequests] = React.useState<string[]>([])

  React.useEffect(() => {
    let cancelled = false
    void fetchDashboardConfig(reportId)
      .then((cfg) => {
        if (cancelled) return
        setPanels(cfg.panels)
        setRowIds(cfg.panels.map(() => newRowId()))
        setTitle(cfg.title ?? "")
        setParameters(cfg.parameters ?? [])
        setLoadedParameters(JSON.stringify(cfg.parameters ?? []))
        setBaseline(
          JSON.stringify({ title: cfg.title ?? "", panels: cfg.panels })
        )
      })
      .catch((err) => {
        if (!cancelled) setLoadError(readDetail(err))
      })
    return () => {
      cancelled = true
    }
  }, [reportId])

  const parametersChanged = JSON.stringify(parameters) !== loadedParameters
  const isDirty =
    panels !== null &&
    (JSON.stringify({ title, panels }) !== baseline || parametersChanged)

  /* The draft as it is right now, read when a message is sent and again when
     its answer arrives — a ref, so neither reads a render-old copy. */
  const draftRef = React.useRef<DashboardDraft | null>(null)
  React.useLayoutEffect(() => {
    draftRef.current = panels === null ? null : { title, panels, parameters }
  }, [title, panels, parameters])

  const getDraft = React.useCallback(
    () => draftRef.current ?? { title: "", panels: [], parameters: [] },
    []
  )

  const onConversation = React.useCallback((id: string | null) => {
    assistantConversation.current = id
  }, [])

  const applyAssistant = React.useCallback(
    (result: DashboardAssistResult, sent: DashboardDraft, request: string) => {
      if (result.status !== "applied" || !result.plan || !result.origins) return true
      // Edited by hand while the assistant worked: applying its answer would
      // silently throw those edits away.
      if (JSON.stringify(draftRef.current) !== JSON.stringify(sent)) return false

      const nextRowIds = result.origins.map((origin) =>
        origin === null ? newRowId() : (rowIds[origin] ?? newRowId())
      )
      setUndoStack((stack) => [
        ...stack,
        { title, panels: sent.panels, parameters, rowIds, aiChanged },
      ])
      setTitle(result.plan.title)
      setPanels(result.plan.panels)
      setParameters(result.plan.parameters ?? [])
      setRowIds(nextRowIds)
      setAiChanged((current) => {
        const next = new Set(current)
        nextRowIds.forEach((id, i) => {
          if (result.changed?.[i]) next.add(id)
        })
        return next
      })
      setAiRequests((current) => [...current, request])
      setSaveError(null)
      const firstChanged = result.changed?.findIndex(Boolean) ?? -1
      if (firstChanged >= 0) setOpenPanel(firstChanged)
      return true
    },
    [rowIds, title, parameters, aiChanged]
  )

  function undoAssistant() {
    const previous = undoStack[undoStack.length - 1]
    if (!previous) return
    setUndoStack(undoStack.slice(0, -1))
    setTitle(previous.title)
    setPanels(previous.panels)
    setParameters(previous.parameters)
    setRowIds(previous.rowIds)
    setAiChanged(previous.aiChanged)
    setAiRequests((current) => current.slice(0, -1))
  }

  /* A panel with no SQL is refused by the server, and the round trip only tells
     you what you already knew. Say it here instead. */
  const panelsWithoutSql = (panels ?? [])
    .map((p, i) => ({ name: p.title || `Panel ${i + 1}`, empty: !p.sql?.trim() }))
    .filter((p) => p.empty)
    .map((p) => p.name)

  /* The server names a failing panel as 'Title': reason. */
  const failingPanel =
    saveError && panels
      ? panels.findIndex((p) => Boolean(p.title) && saveError.includes(`'${p.title}':`))
      : -1

  function showPanel(index: number) {
    setOpenPanel(index)
    // After the panel has opened, so the scroll lands on where it ended up.
    window.setTimeout(() => {
      document
        .querySelector(`[data-editor-panel="${index}"]`)
        ?.scrollIntoView({ block: "start", behavior: "smooth" })
    }, 50)
  }

  function patch(index: number, changes: Partial<DashboardPanelConfig>) {
    setPanels((current) =>
      (current ?? []).map((p, i) => (i === index ? { ...p, ...changes } : p))
    )
  }

  function requestClose() {
    if (isDirty) setConfirmDiscard(true)
    else onClose()
  }

  async function save() {
    if (!panels) return
    setSaving(true)
    setSaveError(null)
    try {
      const saved = panels.map(withoutStaleSeries)
      const editNote =
        aiRequests.length > 0
          ? `Edited with the assistant: ${aiRequests.join("; ")}`.slice(0, 500)
          : ""
      const result = await updateDashboard(
        reportId,
        // Parameters are sent only when the assistant changed them: the server
        // keeps the saved ones otherwise, as it always has.
        { title, panels: saved, ...(parametersChanged ? { parameters } : {}) },
        editNote,
        // Noted in the assistant's thread only when this save carries its
        // changes: a save of hand edits alone is not part of that thread, and
        // a stray note there reads as the assistant's work being saved.
        aiRequests.length > 0 ? assistantConversation.current : null
      )
      const warnings = result.warnings ?? []
      if (warnings.length > 0) {
        toast.warning(`Saved as version ${result.version}, but some charts cannot draw`, {
          description: warnings.join("\n"),
          duration: 15000,
        })
      } else {
        toast.success("Dashboard updated", {
          description: `Saved as version ${result.version}.`,
        })
      }
      setPanels(saved)
      setBaseline(JSON.stringify({ title, panels: saved }))
      setLoadedParameters(JSON.stringify(parameters))
      setUndoStack([])
      setAiChanged(new Set())
      setAiRequests([])
      await onSaved()
      onClose()
    } catch (err) {
      // Kept on screen, not in a toast: this is nearly always a specific
      // panel's SQL failing to compile, and the message names which.
      setSaveError(readDetail(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col border-l bg-background">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-medium">Edit configuration</h2>
          <p className="text-xs text-muted-foreground">
            Saved as a new version — the current one is kept.
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={requestClose}
          aria-label="Close editor"
        >
          <XIcon className="size-4" />
        </Button>
      </div>

      <div className="scrollbar-slim min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {loadError ? (
          <p className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
            {loadError}
          </p>
        ) : panels === null ? (
          <p className="text-xs text-muted-foreground">
            Reading the configuration…
          </p>
        ) : (
          <>
            <div className="space-y-1.5">
              <Label
                htmlFor="dash-title"
                className="text-xs text-muted-foreground"
              >
                Dashboard title
              </Label>
              <Input
                id="dash-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="h-9"
              />
            </div>

            <ul className="divide-y overflow-hidden rounded-lg border">
              {panels.map((panel, i) => (
                <PanelRow
                  key={rowIds[i] ?? i}
                  panel={panel}
                  index={i}
                  open={openPanel === i}
                  failed={failingPanel === i}
                  changedByAssistant={aiChanged.has(rowIds[i])}
                  onToggle={() => setOpenPanel((k) => (k === i ? null : i))}
                  onChange={(changes) => patch(i, changes)}
                  onRemove={
                    panels.length > 1
                      ? () => {
                          setPanels(panels.filter((_, k) => k !== i))
                          setRowIds(rowIds.filter((_, k) => k !== i))
                          setOpenPanel(null)
                        }
                      : undefined
                  }
                />
              ))}
            </ul>

            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => {
                setPanels([...panels, emptyPanel()])
                setRowIds([...rowIds, newRowId()])
                setOpenPanel(panels.length)
              }}
            >
              <PlusIcon className="size-4" />
              Add panel
            </Button>

            {panelsWithoutSql.length > 0 ? (
              <p className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-500">
                <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
                <span>
                  {panelsWithoutSql.join(", ")}{" "}
                  {panelsWithoutSql.length === 1 ? "has" : "have"} no SQL. Every
                  panel needs a query before this can be saved.
                </span>
              </p>
            ) : null}

            {saveError ? (
              <p
                ref={saveErrorRef}
                role="alert"
                className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs whitespace-pre-wrap text-destructive"
              >
                <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
                <span className="min-w-0 flex-1">{saveError}</span>
                {failingPanel >= 0 ? (
                  <Button
                    variant="outline"
                    size="sm"
                    className="shrink-0"
                    onClick={() => showPanel(failingPanel)}
                  >
                    Open panel
                  </Button>
                ) : null}
              </p>
            ) : null}
          </>
        )}
      </div>

      {/* Opening the assistant gives it the lower part of the panel; the form
          above keeps scrolling in what is left. Kept mounted while hidden, so
          collapsing it mid-answer neither loses the answer nor the thread. */}
      {panels !== null ? (
        // Set apart from the form as a card of its own, so its messages never
        // read as part of the configuration being edited. Its top edge drags.
        <ResizableDock
          storageKey="dashboard-edit-assistant-height"
          defaultHeightClassName="h-[48%]"
          hidden={!assistantOpen}
          className="border-t bg-muted/40 px-3 pb-3"
        >
          <DashboardEditAssistant
            reportId={reportId}
            open={assistantOpen}
            getDraft={getDraft}
            onResult={applyAssistant}
            canUndo={undoStack.length > 0}
            onUndo={undoAssistant}
            unsavedChanges={aiRequests.length}
            onConversation={onConversation}
            onCollapse={() => setAssistantOpen(false)}
          />
        </ResizableDock>
      ) : null}

      <div className="flex items-center gap-2 border-t px-4 py-3">
        <Button
          onClick={save}
          disabled={saving || !panels || panelsWithoutSql.length > 0}
          className={cn(
            "gap-2",
            // Unsaved assistant changes: point at the one button that keeps them.
            aiRequests.length > 0 && "ring-2 ring-violet-500/50 ring-offset-2 ring-offset-background"
          )}
        >
          {saving ? <Loader2Icon className="size-4 animate-spin" /> : null}
          Save and refresh
        </Button>
        <Button variant="ghost" onClick={requestClose} disabled={saving}>
          Cancel
        </Button>
        {/* Why nothing happened, next to the button that was pressed. */}
        <span className="min-w-0 flex-1 truncate text-xs">
          {saveError ? (
            <span className="font-medium text-destructive">
              Not saved — see the message above.
            </span>
          ) : panelsWithoutSql.length > 0 ? (
            <span className="text-amber-700 dark:text-amber-500">
              Add SQL to {panelsWithoutSql.join(", ")} to save.
            </span>
          ) : null}
        </span>
        <Button
          size="sm"
          className={cn(
            "shrink-0 gap-1.5 border-0 text-white shadow-sm transition-all",
            assistantOpen
              ? "bg-violet-700 hover:bg-violet-800"
              : "bg-gradient-to-r from-violet-600 to-fuchsia-600 hover:from-violet-700 hover:to-fuchsia-700 hover:shadow-md"
          )}
          aria-expanded={assistantOpen}
          disabled={!panels}
          onClick={() => setAssistantOpen((open) => !open)}
        >
          <SparklesIcon className="size-4" />
          {assistantOpen ? "Hide AI" : "Edit with AI"}
        </Button>
      </div>

      {/* Closing is one click and the edits are only in this component's state,
          so leaving without asking loses however long someone spent on a query. */}
      <Dialog open={confirmDiscard} onOpenChange={setConfirmDiscard}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Discard your changes?</DialogTitle>
            <DialogDescription>
              This dashboard has unsaved edits. Closing now throws them away.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmDiscard(false)}>
              Keep editing
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setConfirmDiscard(false)
                onClose()
              }}
            >
              Discard
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

type PanelTab = "chart" | "format" | "data"

function PanelRow({
  panel,
  index,
  open,
  failed,
  changedByAssistant = false,
  onToggle,
  onChange,
  onRemove,
}: {
  panel: DashboardPanelConfig
  index: number
  open: boolean
  /** The last save was rejected because of this panel. */
  failed: boolean
  /** The assistant changed this panel since the draft was last saved. */
  changedByAssistant?: boolean
  onToggle: () => void
  onChange: (changes: Partial<DashboardPanelConfig>) => void
  /** Absent for the last remaining panel — a dashboard with no panels is
   *  refused by the server anyway, so the control should not offer it. */
  onRemove?: () => void
}) {
  const fieldId = React.useId()
  const chartType = panel.chart_type ?? ""
  const extras = CHART_FIELDS[chartType] ?? []
  const labels = axisLabels(chartType)
  const usesAxes = !IGNORES_AXES.has(chartType)
  const aliases = React.useMemo(() => sqlAliases(panel.sql), [panel.sql])
  // Columns that could size a bubble: any the query returns that is not
  // already an axis or the split, plus whatever is stored, so it never shows blank.
  const used = new Set([panel.x, ...(Array.isArray(panel.y) ? panel.y : [panel.y]), panel.color])
  const sizeChoices = [
    ...aliases.filter((name) => !used.has(name)),
    ...(panel.size && !aliases.includes(panel.size) ? [panel.size] : []),
  ]
  const listId = `${fieldId}-cols`
  const [confirmRemove, setConfirmRemove] = React.useState(false)
  const [tab, setTab] = React.useState<PanelTab>("chart")
  const hasFormat = hasFormatSettings(chartType)
  const activeTab = tab === "format" && !hasFormat ? "chart" : tab
  const missingSql = !panel.sql?.trim()

  /* A rejected save is nearly always the query, so the row opens on it. */
  const [shownFailed, setShownFailed] = React.useState(failed)
  if (failed !== shownFailed) {
    setShownFailed(failed)
    if (failed) setTab("data")
  }

  /* The y field edits as text so a half-typed list is not rewritten under the
     cursor. Committed as a value on every keystroke; the text is dropped on
     blur so the input goes back to showing what is actually stored. */
  const [yDraft, setYDraft] = React.useState<string | null>(null)

  /**
   * Switching chart type clears the columns the new type does not read.
   *
   * A `size` left over from a bubble is invisible on a line chart and comes
   * back the moment someone switches back, which reads as the editor inventing
   * a value nobody typed.
   */
  function changeChartType(value: string) {
    const next = CHART_FIELDS[value] ?? []
    onChange({
      chart_type: value || null,
      color: next.includes("color") ? panel.color : null,
      size: next.includes("size") ? panel.size : null,
      orientation: next.includes("orientation") ? panel.orientation : null,
      // Same reasoning as the columns above: an axis setting left behind on a
      // pie chart is invisible, and reappears the moment someone switches back.
      zero_baseline: FITTABLE_AXIS.has(value) ? panel.zero_baseline : null,
      palette: PALETTED.has(value) ? panel.palette : null,
      format: formatForType(value, panel.format, panel),
    })
  }

  /** Merge into the panel's format; a setting set back to automatic is removed. */
  function setFormat(changes: Partial<PanelFormat>) {
    const merged: Record<string, unknown> = { ...(panel.format ?? {}), ...changes }
    for (const key of Object.keys(merged)) {
      if (merged[key] === undefined) delete merged[key]
    }
    onChange({
      format: Object.keys(merged).length > 0 ? (merged as PanelFormat) : null,
    })
  }

  const format = panel.format ?? {}
  const seriesShapes = panelSeriesShapes(chartType, panel.y, panel.color)
  // Two or more value columns are set series by series; otherwise the chart's
  // one set of label settings is its only series' settings anyway.
  const perSeries = seriesShapes.length > 0
  const numericAxes = chartType === "scatter" || chartType === "bubble"

  return (
    <li data-editor-panel={index}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-muted/50",
          open && "bg-muted/40"
        )}
      >
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {panel.title || `Panel ${index + 1}`}
        </span>
        {changedByAssistant ? (
          <span
            className="inline-flex shrink-0 items-center gap-1 rounded-full border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 text-[10px] text-violet-700 dark:text-violet-300"
            title="Changed by the assistant — not saved yet"
          >
            <SparklesIcon className="size-3" />
            AI
          </span>
        ) : null}
        <span className="shrink-0 rounded-full border bg-background px-2 py-0.5 text-[10px] text-muted-foreground">
          {chartTypeName(panel.chart_type)}
        </span>
        <ChevronRightIcon
          className={cn(
            "size-4 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90"
          )}
        />
      </button>

      {open ? (
        <div className="border-t px-3 pt-3 pb-2">
          <Tabs value={activeTab} onValueChange={(v) => setTab(v as PanelTab)}>
            <TabsList className="w-full">
              <TabsTrigger value="chart" className="text-xs">
                Chart
              </TabsTrigger>
              <TabsTrigger value="format" className="text-xs" disabled={!hasFormat}>
                Format
              </TabsTrigger>
              <TabsTrigger value="data" className="text-xs">
                Data
                {missingSql || failed ? (
                  <span
                    className="size-1.5 rounded-full bg-destructive"
                    aria-label={missingSql ? "No SQL" : "Save failed"}
                  />
                ) : null}
              </TabsTrigger>
            </TabsList>

            <TabsContent value="chart" className="space-y-4 pt-2">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Title" htmlFor={`${fieldId}-title`} wide>
                  <Input
                    id={`${fieldId}-title`}
                    value={panel.title ?? ""}
                    onChange={(e) => onChange({ title: e.target.value })}
                    className="h-8 text-xs"
                  />
                </Field>
                <Field label="Chart type" htmlFor={`${fieldId}-type`}>
                  <select
                    id={`${fieldId}-type`}
                    value={chartType}
                    onChange={(e) => changeChartType(e.target.value)}
                    className={SELECT_CLASS}
                  >
                    {/* A table is the absence of a chart, and without this there is
                        no way back to one once a type has been chosen. */}
                    <option value="">Table (no chart)</option>
                    {CHART_TYPES.map((t) => (
                      <option key={t} value={t}>
                        {chartTypeName(t)}
                      </option>
                    ))}
                  </select>
                </Field>
                {extras.includes("orientation") ? (
                  <Field label="Bar direction" htmlFor={`${fieldId}-orientation`}>
                    <select
                      id={`${fieldId}-orientation`}
                      /* "v" is displayed as auto because that is what it does —
                         the renderer treats the two identically. Left as-is the
                         select would match no option at all, render blank, and the
                         next change would rewrite the panel from an empty state. */
                      value={panel.orientation === "v" ? "" : panel.orientation ?? ""}
                      onChange={(e) =>
                        onChange({ orientation: e.target.value || null })
                      }
                      className={SELECT_CLASS}
                    >
                      <option value="">Auto</option>
                      <option value="vertical">Vertical</option>
                      <option value="h">Horizontal</option>
                    </select>
                  </Field>
                ) : null}
              </div>

              {usesAxes ? (
                <Section title="Columns">
                  <Field label={labels.x} htmlFor={`${fieldId}-x`}>
                    <Input
                      id={`${fieldId}-x`}
                      list={listId}
                      value={panel.x ?? ""}
                      onChange={(e) => onChange({ x: e.target.value || null })}
                      className="h-8 font-mono text-xs"
                    />
                  </Field>
                  <Field label={labels.y} htmlFor={`${fieldId}-y`}>
                    <Input
                      id={`${fieldId}-y`}
                      list={listId}
                      value={yDraft ?? yToText(panel.y)}
                      onChange={(e) => {
                        setYDraft(e.target.value)
                        onChange({ y: parseY(e.target.value) })
                      }}
                      onBlur={() => setYDraft(null)}
                      className="h-8 font-mono text-xs"
                    />
                  </Field>

                  {extras.includes("color") ? (
                    <Field
                      label={
                        chartType === "heatmap" ? "Row column" : "Split into series by"
                      }
                      htmlFor={`${fieldId}-color`}
                      hint={
                        chartType === "heatmap"
                          ? "Its values become the rows of the grid."
                          : "A column. Each value becomes its own series."
                      }
                    >
                      <Input
                        id={`${fieldId}-color`}
                        list={listId}
                        placeholder="Optional"
                        value={panel.color ?? ""}
                        onChange={(e) => onChange({ color: e.target.value || null })}
                        className="h-8 font-mono text-xs"
                      />
                    </Field>
                  ) : null}

                  {extras.includes("size") ? (
                    <Field
                      label="Size column"
                      htmlFor={`${fieldId}-size`}
                      hint="A numeric column. Bubble area follows its value."
                    >
                      <select
                        id={`${fieldId}-size`}
                        value={panel.size ?? ""}
                        onChange={(e) => onChange({ size: e.target.value || null })}
                        className={cn(SELECT_CLASS, "font-mono")}
                      >
                        <option value="">Off</option>
                        {sizeChoices.map((name) => (
                          <option key={name} value={name}>
                            {name}
                          </option>
                        ))}
                      </select>
                    </Field>
                  ) : null}
                </Section>
              ) : (
                <Note>
                  {chartType === "kpi"
                    ? "KPI tiles read the METRIC, VALUE and FORMAT columns of the query."
                    : "A table shows the query's rows as they come back."}
                </Note>
              )}
            </TabsContent>

            <TabsContent value="format" className="space-y-4 pt-2">
              {AXIS_TITLED.has(chartType) || FITTABLE_AXIS.has(chartType) ? (
                <Section title="Axes">
                  {AXIS_TITLED.has(chartType) ? (
                    <>
                      <TitleField
                        id={`${fieldId}-x-title`}
                        label={numericAxes ? "X axis title" : "Category axis title"}
                        value={format.x_title}
                        onChange={(x_title) => setFormat({ x_title })}
                      />
                      <TitleField
                        id={`${fieldId}-y-title`}
                        label={numericAxes ? "Y axis title" : "Value axis title"}
                        value={format.y_title}
                        onChange={(y_title) => setFormat({ y_title })}
                      />
                    </>
                  ) : null}
                  {FITTABLE_AXIS.has(chartType) ? (
                    <Field label="Value axis range" htmlFor={`${fieldId}-zero`}>
                      <select
                        id={`${fieldId}-zero`}
                        value={panel.zero_baseline === false ? "fit" : ""}
                        onChange={(e) =>
                          onChange({
                            zero_baseline: e.target.value === "fit" ? false : null,
                          })
                        }
                        className={SELECT_CLASS}
                      >
                        <option value="">Start at zero</option>
                        <option value="fit">Fit to the data</option>
                      </select>
                    </Field>
                  ) : null}
                  {AXIS_TITLED.has(chartType) ? (
                    <Field label="Number units" htmlFor={`${fieldId}-unit`}>
                      <select
                        id={`${fieldId}-unit`}
                        value={format.unit ?? ""}
                        onChange={(e) =>
                          setFormat({ unit: (e.target.value || undefined) as Unit | undefined })
                        }
                        className={SELECT_CLASS}
                      >
                        <option value="">Auto</option>
                        {UNIT_CHOICES.map((u) => (
                          <option key={u} value={u}>
                            {UNIT_NAMES[u]}
                          </option>
                        ))}
                      </select>
                    </Field>
                  ) : null}
                  {NAMED_POINTS.has(chartType) ? (
                    <>
                      <BoundsField
                        id={`${fieldId}-x-bounds`}
                        label="X axis min / max"
                        min={format.x_min}
                        max={format.x_max}
                        onChange={(x_min, x_max) => setFormat({ x_min, x_max })}
                      />
                      <BoundsField
                        id={`${fieldId}-y-bounds`}
                        label="Y axis min / max"
                        min={format.y_min}
                        max={format.y_max}
                        onChange={(y_min, y_max) => setFormat({ y_min, y_max })}
                      />
                    </>
                  ) : null}
                </Section>
              ) : null}

              {NAMED_POINTS.has(chartType) ? (
                <Section title="Reference lines and trend">
                  <ReferenceField
                    id={`${fieldId}-x-reference`}
                    label="Vertical line (X)"
                    value={format.x_reference}
                    onChange={(x_reference) => setFormat({ x_reference })}
                  />
                  <ReferenceField
                    id={`${fieldId}-y-reference`}
                    label="Horizontal line (Y)"
                    value={format.y_reference}
                    onChange={(y_reference) => setFormat({ y_reference })}
                  />
                  <label
                    htmlFor={`${fieldId}-trend`}
                    className="flex items-center gap-2 text-xs sm:col-span-2"
                  >
                    <Checkbox
                      id={`${fieldId}-trend`}
                      checked={format.trend_line === true}
                      onCheckedChange={(checked) =>
                        setFormat({ trend_line: checked === true ? true : undefined })
                      }
                    />
                    Trend line (best fit)
                  </label>
                </Section>
              ) : null}

              {LABELLED.has(chartType) || AXIS_TITLED.has(chartType) ? (
                <Section
                  title={perSeries && chartType !== "stacked_bar" ? "Legend" : "Labels and legend"}
                >
                  {LABELLED.has(chartType) && !perSeries ? (
                    <Field
                      label={NAMED_POINTS.has(chartType) ? "Point names" : "Value labels"}
                      htmlFor={`${fieldId}-labels`}
                    >
                      <select
                        id={`${fieldId}-labels`}
                        value={format.data_labels ?? ""}
                        onChange={(e) =>
                          setFormat({
                            data_labels: (e.target.value || undefined) as DataLabels | undefined,
                          })
                        }
                        className={SELECT_CLASS}
                      >
                        <option value="">Auto</option>
                        {DATA_LABELS_CHOICES.map((d) => (
                          <option key={d} value={d}>
                            {DATA_LABEL_NAMES[d]}
                          </option>
                        ))}
                      </select>
                    </Field>
                  ) : null}
                  {BAR_POSITIONED.has(chartType) && !perSeries ? (
                    <Field label="Bar label position" htmlFor={`${fieldId}-position`}>
                      <select
                        id={`${fieldId}-position`}
                        value={format.label_position ?? "auto"}
                        onChange={(e) =>
                          setFormat({
                            label_position:
                              e.target.value === "auto" ? undefined : (e.target.value as LabelPosition),
                          })
                        }
                        className={SELECT_CLASS}
                      >
                        {LABEL_POSITION_CHOICES.map((choice) => (
                          <option key={choice} value={choice}>
                            {LABEL_POSITION_NAMES[choice]}
                          </option>
                        ))}
                      </select>
                    </Field>
                  ) : null}
                  {MARKED.has(chartType) && !perSeries ? (
                    <Field label="Line markers" htmlFor={`${fieldId}-markers`}>
                      <select
                        id={`${fieldId}-markers`}
                        value={format.markers ?? "auto"}
                        onChange={(e) =>
                          setFormat({
                            markers: e.target.value === "auto" ? undefined : (e.target.value as Markers),
                          })
                        }
                        className={SELECT_CLASS}
                      >
                        {MARKERS_CHOICES.map((choice) => (
                          <option key={choice} value={choice}>
                            {MARKER_NAMES[choice]}
                          </option>
                        ))}
                      </select>
                    </Field>
                  ) : null}
                  {AXIS_TITLED.has(chartType) ? (
                    <Field label="Legend" htmlFor={`${fieldId}-legend`}>
                      <select
                        id={`${fieldId}-legend`}
                        value={format.legend ?? ""}
                        onChange={(e) =>
                          setFormat({
                            legend: (e.target.value || undefined) as LegendPosition | undefined,
                          })
                        }
                        className={SELECT_CLASS}
                      >
                        <option value="">Auto</option>
                        {LEGEND_CHOICES.map((l) => (
                          <option key={l} value={l}>
                            {LEGEND_NAMES[l]}
                          </option>
                        ))}
                      </select>
                    </Field>
                  ) : null}
                  {chartType === "stacked_bar" ? (
                    <label
                      htmlFor={`${fieldId}-totals`}
                      className="flex items-center gap-2 self-end pb-2 text-xs"
                    >
                      <Checkbox
                        id={`${fieldId}-totals`}
                        checked={format.stack_totals === true}
                        onCheckedChange={(checked) =>
                          setFormat({ stack_totals: checked === true ? true : undefined })
                        }
                      />
                      Show stack totals
                    </label>
                  ) : null}
                </Section>
              ) : null}

              {perSeries ? (
                <Section title="Series">
                  <SeriesLabelSettings
                    idPrefix={fieldId}
                    chartType={chartType}
                    shapes={seriesShapes}
                    format={format}
                    palette={panel.palette}
                    nameOf={(key) => seriesLabel(key, panel.column_formats)}
                    onChange={(series) => setFormat({ series })}
                  />
                </Section>
              ) : null}

              {chartType === "funnel" ? (
                <Section title="Labels">
                  <Field label="Funnel percentages" htmlFor={`${fieldId}-funnel-percent`}>
                    <select
                      id={`${fieldId}-funnel-percent`}
                      value={format.funnel_percent ?? "first"}
                      onChange={(e) =>
                        setFormat({
                          // The default is stored as absent, like every other setting.
                          funnel_percent:
                            e.target.value === "first"
                              ? undefined
                              : (e.target.value as FunnelPercent),
                        })
                      }
                      className={SELECT_CLASS}
                    >
                      {FUNNEL_PERCENT_CHOICES.map((choice) => (
                        <option key={choice} value={choice}>
                          {FUNNEL_PERCENT_NAMES[choice]}
                        </option>
                      ))}
                    </select>
                  </Field>
                </Section>
              ) : null}

              {PALETTED.has(chartType) ? (
                <Section title="Colours">
                  <Field label="Colour set" htmlFor={`${fieldId}-palette`}>
                    <select
                      id={`${fieldId}-palette`}
                      value={panel.palette ?? ""}
                      onChange={(e) => onChange({ palette: e.target.value || null })}
                      className={SELECT_CLASS}
                    >
                      {PALETTE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                  {panel.palette ? (
                    <div className="flex items-end gap-1 pb-2.5" aria-hidden="true">
                      {(PALETTES[panel.palette] ?? []).map((hex) => (
                        <span
                          key={hex}
                          className="h-3 flex-1 rounded-[2px]"
                          style={{ background: hex }}
                        />
                      ))}
                    </div>
                  ) : null}
                </Section>
              ) : null}
            </TabsContent>

            <TabsContent value="data" className="space-y-4 pt-2">
              <Field label="SQL" htmlFor={`${fieldId}-sql`}>
                <Textarea
                  id={`${fieldId}-sql`}
                  value={panel.sql ?? ""}
                  onChange={(e) => onChange({ sql: e.target.value })}
                  spellCheck={false}
                  className="min-h-40 bg-background font-mono text-xs"
                />
              </Field>
              <p className="-mt-2 text-[11px] leading-relaxed text-muted-foreground">
                {aliases.length > 0 ? (
                  <>
                    Returns <span className="font-mono">{aliases.join(", ")}</span>
                  </>
                ) : (
                  "Column names on the Chart tab must match what this query returns."
                )}
              </p>

              {chartType === "kpi" ? (
                /* KPI tiles never read column formats: each tile takes its format
                   from the FORMAT value in its own row, and a KPI's "Revenue" is a
                   row, not a column, so a column format could never match it. The
                   editor used to offer one anyway; a format set there saved, did
                   nothing, and read as lost. Anything already stored is kept. */
                <Note>
                  Each tile takes its format from the <span className="font-mono">FORMAT</span>{" "}
                  value in its row: <span className="font-mono">&apos;currency:AED&apos;</span>,{" "}
                  <span className="font-mono">&apos;percent&apos;</span>,{" "}
                  <span className="font-mono">&apos;count&apos;</span> or{" "}
                  <span className="font-mono">&apos;ratio&apos;</span>.
                </Note>
              ) : (
                <ColumnFormatsField
                  value={panel.column_formats ?? null}
                  onChange={(column_formats) => onChange({ column_formats })}
                />
              )}
            </TabsContent>
          </Tabs>

          {/* Offered to every column field. Retyping an alias from memory and
              finding out at save time, from the warehouse, was the slowest
              possible way to catch a typo. */}
          <datalist id={listId}>
            {aliases.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>

          {onRemove ? (
            /* Two clicks rather than a modal: the panel only leaves this draft,
               so Cancel still gets it back, but a single misclick should not
               empty a row of the form. The second click needs a way out that is
               not "click somewhere else and hope" — hence the explicit Cancel. */
            <div className="mt-3 flex items-center justify-end gap-1 border-t pt-2">
              {confirmRemove ? (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => setConfirmRemove(false)}
                >
                  Cancel
                </Button>
              ) : null}
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  if (confirmRemove) onRemove()
                  else setConfirmRemove(true)
                }}
                className="h-7 gap-1.5 text-xs text-destructive hover:bg-destructive/10 hover:text-destructive"
              >
                <Trash2Icon className="size-3.5" />
                {confirmRemove ? "Click again to remove" : "Remove panel"}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  )
}

const SELECT_CLASS = "h-8 w-full rounded-md border bg-background px-2 text-xs"

/**
 * Label settings listed series by series.
 *
 * Each row shows what that series actually draws, and a change applies to
 * that series alone. There is no chart-wide value to override: with one, a
 * series left alone still followed it, and "All" on the chart kept labelling
 * bars after the line had been set on its own.
 */
function SeriesLabelSettings({
  idPrefix,
  chartType,
  shapes,
  format,
  palette,
  nameOf,
  onChange,
}: {
  idPrefix: string
  chartType: string
  shapes: SeriesShape[]
  format: PanelFormat
  palette: string | null | undefined
  nameOf: (key: string) => string
  onChange: (next: Record<string, SeriesFormat> | undefined) => void
}) {
  const all = format.series ?? {}
  const drawn = new Set(shapes.map((s) => s.key))
  const stale = Object.keys(all).filter((key) => !drawn.has(key))

  function update(key: string, changes: Partial<SeriesFormat>) {
    const next = { ...all, [key]: { ...(all[key] ?? {}), ...changes } }
    onChange(next)
  }

  // The colours the chart gives each series, so a row can be matched to it.
  const colours: string[] = []
  shapes.forEach((shape, i) => {
    const accent = (chartType === "combo" || chartType === "area_line") && i === shapes.length - 1
    colours.push(accent ? accentColor(palette, colours) : seriesColor(i, shape.key, palette))
  })

  const kindName = (shape: SeriesShape) =>
    shape.reference ? "Target line"
    : shape.kind === "bar" ? "Bars"
    : shape.kind === "segment" ? "Segments"
    : chartType === "area" || chartType === "area_line" ? "Area"
    : "Line"

  return (
    <div className="space-y-2 sm:col-span-2">
      {shapes.map((shape, i) => {
        const own = all[shape.key] ?? {}
        const labels =
          own.data_labels ??
          (shape.reference ? "none" : dataLabelsFor(chartType, 0, format.data_labels))
        const markers = own.markers ?? (shape.reference ? "hide" : format.markers ?? "auto")
        const position = seriesLabelPosition(format, shape.key)
        const id = `${idPrefix}-series-${i}`
        return (
          <div key={shape.key} className="space-y-2 rounded-md border bg-muted/20 p-2.5">
            <p className="flex items-center gap-2 text-xs font-medium">
              <span
                className="size-2.5 shrink-0 rounded-[2px]"
                style={{ background: colours[i] }}
                aria-hidden="true"
              />
              <span className="min-w-0 truncate">{nameOf(shape.key)}</span>
              <span className="shrink-0 text-[10px] font-normal text-muted-foreground">
                {kindName(shape)}
              </span>
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="Value labels" htmlFor={`${id}-labels`}>
                <select
                  id={`${id}-labels`}
                  value={labels}
                  onChange={(e) => update(shape.key, { data_labels: e.target.value as DataLabels })}
                  className={SELECT_CLASS}
                >
                  {DATA_LABELS_CHOICES.map((choice) => (
                    <option key={choice} value={choice}>
                      {DATA_LABEL_NAMES[choice]}
                    </option>
                  ))}
                </select>
              </Field>
              {shape.kind === "bar" ? (
                <Field label="Label position" htmlFor={`${id}-position`}>
                  <select
                    id={`${id}-position`}
                    value={position}
                    onChange={(e) =>
                      update(shape.key, { label_position: e.target.value as LabelPosition })
                    }
                    className={SELECT_CLASS}
                  >
                    {LABEL_POSITION_CHOICES.map((choice) => (
                      <option key={choice} value={choice}>
                        {LABEL_POSITION_NAMES[choice]}
                      </option>
                    ))}
                  </select>
                </Field>
              ) : null}
              {shape.kind === "line" ? (
                <Field label="Markers" htmlFor={`${id}-markers`}>
                  <select
                    id={`${id}-markers`}
                    value={markers}
                    onChange={(e) => update(shape.key, { markers: e.target.value as Markers })}
                    className={SELECT_CLASS}
                  >
                    {MARKERS_CHOICES.map((choice) => (
                      <option key={choice} value={choice}>
                        {MARKER_NAMES[choice]}
                      </option>
                    ))}
                  </select>
                </Field>
              ) : null}
            </div>
          </div>
        )
      })}

      {stale.length > 0 ? (
        <p className="text-[11px] text-amber-700 dark:text-amber-500">
          Not a value column any more: {stale.map(nameOf).join(", ")}. Its settings are
          removed when you save.
        </p>
      ) : null}
    </div>
  )
}

/**
 * A number typed as text, committed when it reads as one.
 *
 * Held as text while typing, so "-" or "1." on the way to a number is not
 * wiped by the stored value; the text is dropped on blur.
 */
function NumberInput({
  id,
  value,
  placeholder,
  label,
  onChange,
}: {
  id?: string
  value: number | undefined
  placeholder: string
  label: string
  onChange: (next: number | undefined) => void
}) {
  const [draft, setDraft] = React.useState<string | null>(null)
  return (
    <Input
      id={id}
      aria-label={label}
      inputMode="decimal"
      placeholder={placeholder}
      value={draft ?? (value === undefined ? "" : String(value))}
      onChange={(e) => {
        const text = e.target.value
        setDraft(text)
        if (text.trim() === "") onChange(undefined)
        else if (Number.isFinite(Number(text))) onChange(Number(text))
      }}
      onBlur={() => setDraft(null)}
      className="h-8 min-w-0 text-xs"
    />
  )
}

/** An axis' manual bounds. Empty means the chart standard's. */
function BoundsField({
  id,
  label,
  min,
  max,
  onChange,
}: {
  id: string
  label: string
  min: number | undefined
  max: number | undefined
  onChange: (min: number | undefined, max: number | undefined) => void
}) {
  const invalid = min !== undefined && max !== undefined && min >= max
  return (
    <Field label={label} htmlFor={`${id}-min`}>
      <div className="flex items-center gap-1.5">
        <NumberInput
          id={`${id}-min`}
          label={`${label}: min`}
          placeholder="Auto"
          value={min}
          onChange={(next) => onChange(next, max)}
        />
        <span className="text-xs text-muted-foreground">–</span>
        <NumberInput
          label={`${label}: max`}
          placeholder="Auto"
          value={max}
          onChange={(next) => onChange(min, next)}
        />
      </div>
      {invalid ? (
        <p className="text-[10px] text-amber-700 dark:text-amber-500">
          Min must be below max, or neither is used.
        </p>
      ) : null}
    </Field>
  )
}

const REFERENCE_NAMES: Record<ReferenceKind, string> = {
  mean: "Mean",
  median: "Median",
  value: "Value",
}

/** A reference line across one axis: off, at the mean or median, or a typed value. */
function ReferenceField({
  id,
  label,
  value,
  onChange,
}: {
  id: string
  label: string
  value: AxisReference | undefined
  onChange: (next: AxisReference | undefined) => void
}) {
  return (
    <Field label={label} htmlFor={id}>
      <div className="flex items-center gap-1.5">
        <select
          id={id}
          value={value?.kind ?? ""}
          onChange={(e) => {
            const kind = e.target.value as ReferenceKind | ""
            onChange(kind ? { kind, ...(kind === "value" ? { value: value?.value } : {}) } : undefined)
          }}
          className={SELECT_CLASS}
        >
          <option value="">Off</option>
          {REFERENCE_CHOICES.map((kind) => (
            <option key={kind} value={kind}>
              {REFERENCE_NAMES[kind]}
            </option>
          ))}
        </select>
        {value?.kind === "value" ? (
          <NumberInput
            label={`${label}: value`}
            placeholder="Number"
            value={value.value}
            onChange={(next) => onChange({ kind: "value", value: next })}
          />
        ) : null}
      </div>
    </Field>
  )
}

/** A group of fields under a small heading, two to a row. */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h4 className="text-[10px] font-semibold tracking-wider text-muted-foreground uppercase">
        {title}
      </h4>
      <div className="grid gap-3 sm:grid-cols-2">{children}</div>
    </section>
  )
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-md border border-dashed px-2.5 py-2 text-[11px] text-muted-foreground">
      {children}
    </p>
  )
}

/**
 * An axis title: empty follows the chart standard, and Show unticked hides it.
 * Stored as absent for automatic and "" for hidden.
 */
function TitleField({
  id,
  label,
  value,
  onChange,
}: {
  id: string
  label: string
  value: string | undefined
  onChange: (next: string | undefined) => void
}) {
  const hidden = value === ""
  return (
    <Field label={label} htmlFor={id}>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          value={value ?? ""}
          placeholder={hidden ? "Hidden" : "Auto"}
          disabled={hidden}
          onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
          className="h-8 min-w-0 text-xs"
        />
        <label
          htmlFor={`${id}-show`}
          className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted-foreground"
        >
          <Checkbox
            id={`${id}-show`}
            checked={!hidden}
            onCheckedChange={(checked) => onChange(checked === true ? undefined : "")}
            aria-label={`Show ${label.toLowerCase()}`}
          />
          Show
        </label>
      </div>
    </Field>
  )
}

/**
 * Per-column formatting, authored by hand.
 *
 * The planner can declare these and the renderer honours them, but until now
 * nothing could edit them — so a column rendering as a bare number, or a total
 * already divided into billions and then abbreviated again, could not be
 * corrected without re-planning the whole dashboard.
 *
 * Held as a list rather than an object so a column name can be retyped without
 * the row disappearing between keystrokes; committed back as the map the
 * renderer reads.
 */
function ColumnFormatsField({
  value,
  onChange,
}: {
  value: ColumnFormats | null
  onChange: (next: ColumnFormats | null) => void
}) {
  type Row = { id: string; column: string; spec: ColumnFormat }
  const [rows, setRows] = React.useState<Row[]>(() =>
    Object.entries(value ?? {}).map(([column, spec]) => ({
      id: newRowId(),
      column,
      spec,
    }))
  )

  function commit(next: Row[]) {
    setRows(next)
    const out: ColumnFormats = {}
    for (const row of next) {
      const column = row.column.trim()
      // A row with no column name addresses nothing; keep it on screen while it
      // is being typed, but never store it.
      if (!column) continue
      const spec: ColumnFormat = {}
      if (row.spec.format) spec.format = row.spec.format
      if (typeof row.spec.decimals === "number") spec.decimals = row.spec.decimals
      if (row.spec.label?.trim()) spec.label = row.spec.label.trim()
      // Deliberately `!== undefined` and not a truthiness test: "" is a
      // statement — the author cannot name the currency — and dropping it as
      // falsy would silently restore dollars, the exact bug the field exists
      // to fix. Only meaningful on a currency row, so only carried there.
      if (spec.format === "currency" && row.spec.currency !== undefined) {
        spec.currency = row.spec.currency.trim().toUpperCase()
      }
      if (Object.keys(spec).length > 0) out[column] = spec
    }
    onChange(Object.keys(out).length > 0 ? out : null)
  }

  return (
    <div className="space-y-1.5">
      <Label className="text-[11px] text-muted-foreground">
        Column formats
      </Label>
      {rows.length === 0 ? (
        <p className="text-[11px] text-muted-foreground">
          None. Add one when a column's name does not make its unit obvious.
        </p>
      ) : null}
      <div className="space-y-2">
        {rows.map((row, i) => (
          <div
            key={row.id}
            className={cn(
              "grid gap-1.5",
              // The currency box only exists for a currency row, so the grid
              // has to make room for it only there.
              row.spec.format === "currency"
                ? "grid-cols-[1fr_auto_auto_auto_1fr_auto]"
                : "grid-cols-[1fr_auto_auto_1fr_auto]"
            )}
          >
            <Input
              aria-label="Column name"
              placeholder="COLUMN"
              value={row.column}
              onChange={(e) =>
                commit(rows.map((r, k) => (k === i ? { ...r, column: e.target.value } : r)))
              }
              className="h-8 font-mono text-xs"
            />
            <select
              aria-label="Format"
              value={row.spec.format ?? ""}
              onChange={(e) =>
                commit(
                  rows.map((r, k) =>
                    k === i
                      ? {
                          ...r,
                          spec: {
                            ...r.spec,
                            format: (e.target.value || undefined) as
                              | ColumnFormat["format"]
                              | undefined,
                          },
                        }
                      : r
                  )
                )
              }
              className="h-8 rounded-md border bg-background px-1.5 text-xs"
            >
              <option value="">auto</option>
              {COLUMN_FORMAT_OPTIONS.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
            {row.spec.format === "currency" ? (
              /* Shown ONLY for a currency row. Offered on every row it would
                 invite setting a code nothing reads — the renderer ignores it
                 unless the format is currency.

                 Empty is not the same as unset: leaving this blank keeps the
                 US dollar default, while typing nothing after having declared
                 one is how an author says "I cannot name the currency here",
                 which prints the number with no symbol at all. */
              <Input
                aria-label="Currency code"
                placeholder="USD"
                maxLength={3}
                value={row.spec.currency ?? ""}
                onChange={(e) =>
                  commit(
                    rows.map((r, k) =>
                      k === i
                        ? {
                            ...r,
                            spec: {
                              ...r.spec,
                              currency: e.target.value.toUpperCase(),
                            },
                          }
                        : r
                    )
                  )
                }
                className="h-8 w-16 text-center font-mono text-xs uppercase"
              />
            ) : null}
            <Input
              aria-label="Decimals"
              placeholder="dp"
              inputMode="numeric"
              value={row.spec.decimals ?? ""}
              onChange={(e) => {
                const raw = e.target.value.trim()
                const decimals = raw === "" ? undefined : Number(raw)
                commit(
                  rows.map((r, k) =>
                    k === i
                      ? {
                          ...r,
                          spec: {
                            ...r.spec,
                            decimals:
                              decimals !== undefined && Number.isFinite(decimals)
                                ? Math.max(0, Math.min(6, Math.trunc(decimals)))
                                : undefined,
                          },
                        }
                      : r
                  )
                )
              }}
              className="h-8 w-12 text-center text-xs"
            />
            <Input
              aria-label="Header label"
              placeholder="header label"
              value={row.spec.label ?? ""}
              onChange={(e) =>
                commit(
                  rows.map((r, k) =>
                    k === i ? { ...r, spec: { ...r.spec, label: e.target.value } } : r
                  )
                )
              }
              className="h-8 text-xs"
            />
            <Button
              variant="ghost"
              size="icon"
              className="size-8 text-muted-foreground"
              aria-label={`Remove format for ${row.column || "column"}`}
              onClick={() => commit(rows.filter((_, k) => k !== i))}
            >
              <XIcon className="size-3.5" />
            </Button>
          </div>
        ))}
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1.5 text-xs"
        onClick={() =>
          commit([...rows, { id: newRowId(), column: "", spec: {} }])
        }
      >
        <PlusIcon className="size-3.5" />
        Add column format
      </Button>
    </div>
  )
}

/** The stored y, as the comma-separated text the field edits. */
function yToText(y: string | string[] | null): string {
  return Array.isArray(y) ? y.join(", ") : (y ?? "")
}

/**
 * Text back to a stored y.
 *
 * Empty segments are dropped, so a half-typed "REVENUE," commits as "REVENUE"
 * rather than storing the trailing comma — which it used to, producing a column
 * name that matched nothing and failed only at save time.
 */
function parseY(raw: string): string | string[] | null {
  const parts = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
  if (parts.length === 0) return null
  return parts.length === 1 ? parts[0] : parts
}

function Field({
  label,
  htmlFor,
  hint,
  wide = false,
  children,
}: {
  label: string
  htmlFor?: string
  /** Spans both columns of a two-column grid. */
  wide?: boolean
  /** One line under the control. Present where the field's name alone has
   *  proved not to be enough — which is most of the ones people get wrong. */
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className={cn("min-w-0 space-y-1", wide && "sm:col-span-2")}>
      <Label htmlFor={htmlFor} className="text-[11px] text-muted-foreground">
        {label}
      </Label>
      {children}
      {hint ? (
        <p className="text-[10px] leading-snug text-muted-foreground/80">{hint}</p>
      ) : null}
    </div>
  )
}

/** The server's message when there is one — a failed save names the panel and
 *  the compile error, which is the whole point of reading it. */
function readDetail(err: unknown): string {
  if (!(err instanceof Error)) return "Something went wrong."
  try {
    const parsed = JSON.parse(err.message) as { detail?: string }
    if (typeof parsed.detail === "string") return parsed.detail
  } catch {
    // Not JSON — fall through to the raw message.
  }
  return err.message || "Something went wrong."
}
