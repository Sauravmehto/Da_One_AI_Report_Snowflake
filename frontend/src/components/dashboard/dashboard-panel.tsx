import * as React from "react"
import {
  ArrowDownIcon,
  ArrowUpIcon,
  ChevronRightIcon,
  InfoIcon,
  XIcon,
} from "lucide-react"
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Funnel,
  FunnelChart,
  LabelList,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  Scatter,
  ScatterChart,
  Tooltip,
  Treemap,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ChartContainer } from "@/components/ui/chart"
// Aliased: this file already imports Recharts' own Tooltip for the charts.
import {
  Tooltip as HintTooltip,
  TooltipContent as HintTooltipContent,
  TooltipTrigger as HintTooltipTrigger,
} from "@/components/ui/tooltip"
import {
  Popover,
  PopoverClose,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { describePanel } from "@/lib/api"
import {
  columnDateGrains,
  columnIndex,
  detectDateGranularity,
  formatDateLabel,
  type DateGranularity,
  formatAxis,
  formatCell,
  seriesLabel,
  formatKpi,
  heatColor,
  isNumericColumn,
  rankHeatRows,
  accentColor,
  seriesColor,
  sortHeatLabels,
  toChartData,
  type ColumnFormats,
} from "@/lib/dashboard-format"
import { rowSpanScale } from "@/lib/dashboard-layout"
import {
  axisFormat,
  boundedScale,
  bubbleRadius,
  clipLine,
  dataLabelsFor,
  dateAxisTitle,
  extent,
  formatLabelValue,
  formatTick,
  horizontalLabelRoom,
  LABEL_ROOM,
  labelIndices,
  linearFit,
  markerDot,
  referenceValue,
  seriesLabelPosition,
  seriesLabels,
  seriesMarkers,
  UPRIGHT_LABEL_ROOM,
  legendFor,
  spreadLabels,
  niceScale,
  shareStackScale,
  sizeLegendValues,
  spansYears,
  stackTotals,
  titleCarriesCurrency,
  titleOr,
  valueAxisTitle,
  funnelStageNotes,
  sortFunnelStages,
  type AxisFormat,
  type LegendPosition,
  type PanelFormat,
  type Scale,
} from "@/lib/chart-standard"
import { useChartDisplay } from "@/components/dashboard/chart-display-context"
import { ValueLabels } from "@/components/dashboard/value-labels"
import { GeoPanel } from "@/components/dashboard/geo-panel"
import { cn } from "@/lib/utils"

export type DashboardPanel = {
  title: string
  chart_type: string | null
  x: string | null
  y: string | string[] | null
  color: string | null
  size: string | null
  orientation: string | null
  display: string | null
  columns: string[]
  rows: unknown[][]
  row_count: number
  /* The warehouse stopped at its row cap, so these rows are a prefix of the
     answer rather than the answer. Absent on anything saved or served before
     the flag existed, which reads the same as false. */
  truncated?: boolean | null
  error?: string | null
  /* Layout, all nullable: dashboards saved before tabs and spans existed carry
     none of these, and `@/lib/dashboard-layout` derives a width when they are
     absent. `section` is the tab; `span` is columns out of 4; `row_span` is a
     height step. */
  section?: string | null
  span?: number | null
  row_span?: number | null
  /* Authored per-column formatting. Absent on every dashboard saved before it
     existed, which is why the renderer falls back to classifying by column
     name rather than treating this as required. */
  column_formats?: ColumnFormats | null
  /* Presentation, both absent by default so older dashboards are untouched.
     `zero_baseline: false` lets the value axis fit the data instead of
     anchoring at zero; absent means anchor, which is the long-standing
     behaviour. `palette` names a series palette; absent means the default
     colours plus the semantic status rules. */
  zero_baseline?: boolean | null
  palette?: string | null
  /* The database's reporting currency, attached by run_dashboard. Absent for a
     database that declares none, which renders as US dollars exactly as
     before. A column that states its own code still wins over it. */
  default_currency?: string | null
  /* Label and axis settings from the editor. Absent means the chart standard
     in @/lib/chart-standard applies unchanged. */
  format?: PanelFormat | null
}

/* Panel sizing and placement live in `@/lib/dashboard-layout` — this file
   renders a panel, it does not decide where the panel goes. */

/**
 * "What am I looking at?" for one chart.
 *
 * Written by the model on FIRST OPEN, not when the dashboard is saved, so
 * every panel that already exists has one without being re-saved, and a panel
 * nobody asks about costs nothing. The server caches by a fingerprint of the
 * chart, so the second person to ask any given chart gets it instantly.
 *
 * Only names are sent — never rows. See describePanel in lib/api.
 */
function PanelInfoButton({ panel }: { panel: DashboardPanel }) {
  const [text, setText] = React.useState<string | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(false)

  const load = React.useCallback(() => {
    // Once fetched it is kept for the life of the panel: the explanation
    // describes the chart, which has not changed since it was written.
    if (text || loading) return
    setLoading(true)
    setError(null)
    describePanel({
      title: panel.title,
      chart_type: panel.chart_type ?? "table",
      x: panel.x,
      y: panel.y,
      columns: panel.columns,
    })
      .then((res) => setText(res.description))
      .catch((err) =>
        setError(
          err instanceof Error ? err.message : "Could not load the explanation"
        )
      )
      .finally(() => setLoading(false))
  }, [panel, text, loading])

  return (
    <Popover>
      <PopoverTrigger
        aria-label={`What does "${panel.title}" show?`}
        onClick={load}
        className="shrink-0 rounded-full p-0.5 text-muted-foreground opacity-60 transition hover:bg-muted hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-1"
      >
        <InfoIcon className="size-3.5" />
      </PopoverTrigger>
      <PopoverContent side="right" align="start" sideOffset={8} collisionPadding={12}>
        <div className="mb-1.5 flex items-start justify-between gap-2">
          <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            About this chart
          </p>
          <PopoverClose
            aria-label="Close"
            className="-mr-1 -mt-0.5 shrink-0 rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-1"
          >
            <XIcon className="size-3.5" />
          </PopoverClose>
        </div>
        {loading ? (
          <p className="text-xs text-muted-foreground">Writing an explanation…</p>
        ) : error ? (
          <p className="text-xs text-destructive">{error}</p>
        ) : (
          <p className="text-xs leading-relaxed">{text}</p>
        )}
      </PopoverContent>
    </Popover>
  )
}

function PanelShell({
  panel,
  children,
  bare = false,
}: {
  panel: DashboardPanel
  children: React.ReactNode
  bare?: boolean
}) {
  const shown = panel.rows.length
  const total = panel.row_count ?? shown
  const capped = total > shown

  return (
    // min-w-0 throughout: a grid/flex item defaults to min-width:auto, so a
    // nowrap table inside would set the column's floor and the panel could
    // then never shrink back when the sidebar widens.
    // `h-full` is what makes a row of panels line up. A grid row is as tall as
    // its tallest item, but a card sized to its own content leaves the
    // difference as a visible gap between its bottom edge and the next row —
    // and the difference is real, because a chart whose labels need a deep
    // gutter is genuinely taller than one whose labels do not.
    //
    // The card stretches; the chart inside keeps its intrinsic height (it must
    // — see the ChartContainer note on ResponsiveContainer). So the slack ends
    // up inside the shorter card rather than between the cards, which is the
    // far less noticeable of the two, since the border runs unbroken through
    // it. In normal flow (chat) `h-full` resolves to auto and changes nothing.
    <Card
      className={cn(
        "flex h-full min-w-0 flex-col",
        bare && "border-none bg-transparent shadow-none"
      )}
    >
      <CardHeader className="min-w-0 space-y-0.5 pb-2 pt-4">
        {/* The button sits WITH the title rather than in a corner, so it reads
            as belonging to this chart. `min-w-0` on the title keeps the
            truncation working now that it shares the row. */}
        <div className="flex min-w-0 items-center gap-1.5">
          <CardTitle className="min-w-0 truncate text-[14px] font-semibold leading-snug tracking-tight">
            {panel.title}
          </CardTitle>
          {!bare ? <PanelInfoButton panel={panel} /> : null}
        </div>
        {/* KPI shells are bare and hide this; charts only show a count when
            the query was truncated so the subtitle is a warning, not noise. */}
        {!bare && capped ? (
          <p className="text-xs text-amber-600 dark:text-amber-500">
            Showing {shown.toLocaleString()} of {total.toLocaleString()} rows
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="flex min-h-0 min-w-0 flex-1 flex-col pt-0">
        {children}
      </CardContent>
    </Card>
  )
}

/** One headline number. Rendered from a panel row, wherever that row lives. */
function KpiTile({
  panel,
  row,
  twoLineLabels = false,
}: {
  panel: DashboardPanel
  row: unknown[]
  /* Reserve two lines for the name on every tile in the band, so the values
     stay level when one name wraps and its neighbours do not. */
  twoLineLabels?: boolean
}) {
  const { columns } = panel
  const mi = columnIndex(columns, "METRIC")
  const vi = columnIndex(columns, "VALUE")
  const fi = columnIndex(columns, "FORMAT")
  const si = columnIndex(columns, "SEVERITY")
  const di = columnIndex(columns, "MOM_CHANGE_PCT")
  const sti = columnIndex(columns, "STATUS")
  const metricIdx = mi >= 0 ? mi : 0
  const valueIdx = vi >= 0 ? vi : 1

  // Highlighting comes from the DECLARED severity column, never inferred from
  // the metric's name — a zero breach count must not look alarming.
  const severity =
    si >= 0 ? String(row[si] ?? "").toLowerCase().trim() : ""
  const delta =
    di >= 0 && typeof row[di] === "number" ? (row[di] as number) : null
  const status =
    sti >= 0 && row[sti] != null && String(row[sti]).trim()
      ? String(row[sti]).trim()
      : null

  return (
    <div
      className={cn(
        "relative min-w-0 overflow-hidden rounded-xl border border-border/50 bg-card p-4 shadow-sm",
        severity === "critical" && "border-destructive/35 bg-destructive/[0.06]",
        severity === "warning" && "border-amber-500/35 bg-amber-500/[0.06]"
      )}
    >
      <div
        className={cn(
          "absolute inset-y-0 left-0 w-1",
          severity === "critical"
            ? "bg-destructive"
            : severity === "warning"
              ? "bg-amber-500"
              : "bg-primary/35"
        )}
        aria-hidden
      />
      <p
        className={cn(
          "line-clamp-2 pl-2.5 text-[11px] leading-snug font-medium tracking-wide text-muted-foreground uppercase",
          twoLineLabels && "min-h-[2lh]"
        )}
        title={String(row[metricIdx])}
      >
        {String(row[metricIdx])}
      </p>
      <p className="mt-1.5 truncate pl-2.5 text-[1.65rem] font-semibold tracking-tight tabular-nums text-foreground">
        {formatKpi(
          fi >= 0 ? row[fi] : null,
          String(row[metricIdx]),
          row[valueIdx],
          panel.default_currency
        )}
      </p>
      {delta !== null ? (
        <span
          className={cn(
            "mt-2 ml-2.5 inline-flex items-center rounded-md px-1.5 py-0.5 text-[11px] font-medium tabular-nums",
            delta > 0
              ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
              : delta < 0
                ? "bg-destructive/10 text-destructive"
                : "bg-muted text-muted-foreground"
          )}
        >
          {delta > 0 ? "▲ +" : delta < 0 ? "▼ " : ""}
          {Math.abs(delta).toFixed(1)}% MoM
        </span>
      ) : status ? (
        <p className="mt-2 truncate pl-2.5 text-xs text-muted-foreground">
          {status}
        </p>
      ) : null}
    </div>
  )
}

/**
 * Every headline number on the dashboard, as one strip.
 *
 * The planner emits KPIs inconsistently — sometimes four metrics in one panel,
 * sometimes one metric per panel. Rendered as ordinary panels that meant three
 * stacked full-width cards, two of them holding a single number, before you
 * reached the first chart. Flattening every KPI row into one grid makes the
 * layout independent of how the planner happened to group them.
 */
export function DashboardKpiBand({
  panels,
  className,
}: {
  panels: DashboardPanel[]
  className?: string
}) {
  const tiles = panels.flatMap((panel) =>
    panel.error || panel.rows.length === 0
      ? []
      : panel.rows.map((row, i) => ({ panel, row, key: `${panel.title}-${i}` }))
  )
  if (tiles.length === 0) return null

  // Past this many characters an upper-case name wraps on a narrow tile.
  // Measured against the tiles reported cut off ("PURCHASE CONVERSION RATE").
  const twoLineLabels = tiles.some(({ panel, row }) => {
    const mi = columnIndex(panel.columns, "METRIC")
    return String(row[mi >= 0 ? mi : 0] ?? "").length > 20
  })

  return (
    // Container queries, not viewport breakpoints: the strip must reflow on the
    // width it HAS, which the resizable sidebar changes without the viewport
    // moving. The query element wraps the grid — an element cannot query itself.
    <div className={cn("@container/kpi min-w-0", className)}>
      <div className="grid gap-3 @lg/kpi:grid-cols-2 @3xl/kpi:grid-cols-3 @5xl/kpi:grid-cols-5">
        {tiles.map(({ panel, row, key }) => (
          <KpiTile key={key} panel={panel} row={row} twoLineLabels={twoLineLabels} />
        ))}
      </div>
    </div>
  )
}

function KpiPanel({ panel }: { panel: DashboardPanel }) {
  return <DashboardKpiBand panels={[panel]} />
}

/** Rows a table draws at first, and how many more each scroll to its end adds. */
const TABLE_PAGE = 100

type TableSort = { column: number; direction: "asc" | "desc" }

/**
 * Two cells in the same column, in ascending order. Numbers compare as
 * numbers; everything else — names, ISO dates, codes — as text with digit
 * runs read numerically, so "Store 9" comes before "Store 10".
 */
function compareCells(a: unknown, b: unknown, numeric: boolean): number {
  if (numeric) return Number(a) - Number(b)
  return String(a).localeCompare(String(b), undefined, {
    numeric: true,
    sensitivity: "base",
  })
}

const isEmptyCell = (value: unknown) =>
  value === null || value === undefined || value === ""

function TablePanel({ panel }: { panel: DashboardPanel }) {
  // Per column, so a SALE MONTH column reads 02-2025 like the axis does.
  // Computed once here rather than per cell: it scans every row.
  const dateGrains = React.useMemo(
    () => columnDateGrains(panel.columns, panel.rows),
    [panel.columns, panel.rows]
  )
  const { columns, rows } = panel
  const numeric = React.useMemo(
    () => columns.map((_, i) => isNumericColumn(rows, i)),
    [columns, rows]
  )

  /* Hovering a column header reveals one arrow button, named by what a click
     will do: "Sort ascending" first, then "Sort descending", flipping each
     time. Sorting reorders the rows already here; the query is not re-run. */
  const [sort, setSort] = React.useState<TableSort | null>(null)
  const ordered = React.useMemo(() => {
    if (!sort) return rows
    const { column, direction } = sort
    const sign = direction === "asc" ? 1 : -1
    return rows
      .map((row, index) => ({ row, index }))
      .sort((a, b) => {
        const left = a.row[column]
        const right = b.row[column]
        // Blank cells sit at the bottom whichever way the column is sorted.
        const blanks = Number(isEmptyCell(left)) - Number(isEmptyCell(right))
        if (blanks !== 0) return blanks
        if (isEmptyCell(left)) return a.index - b.index
        return (
          sign * compareCells(left, right, numeric[column]) || a.index - b.index
        )
      })
      .map(({ row }) => row)
  }, [rows, sort, numeric])
  const applySort = (next: TableSort) => {
    setSort(next)
    scrollRef.current?.scrollTo({ top: 0 })
  }

  /* A panel can carry thousands of rows. Drawing them all at once stalls the
     page, so the table grows as it is scrolled: reaching the end loads the
     next rows. */
  const [limit, setLimit] = React.useState(TABLE_PAGE)
  const scrollRef = React.useRef<HTMLDivElement>(null)
  const endRef = React.useRef<HTMLDivElement>(null)
  const shown = Math.min(limit, rows.length)
  const hasMore = rows.length > shown

  React.useEffect(() => {
    const root = scrollRef.current
    const end = endRef.current
    if (!hasMore || !root || !end) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setLimit((current) => current + TABLE_PAGE)
        }
      },
      { root, rootMargin: "0px 0px 160px 0px" }
    )
    observer.observe(end)
    return () => observer.disconnect()
  }, [hasMore, shown])

  return (
    // `w-auto min-w-full` sizes columns to their content and lets the whole
    // table scroll sideways when they do not fit. Plain `w-full` stretched
    // three columns across the panel and parked them at opposite edges.
    <div
      ref={scrollRef}
      className="scrollbar-slim max-w-full overflow-auto overscroll-contain rounded-md border border-border/60"
      style={{ maxHeight: scaledHeight(320, panel) }}
    >
      <table className="w-auto min-w-full text-[12.5px]">
        <thead className="sticky top-0 z-10 bg-muted/90 backdrop-blur">
          <tr>
            {columns.map((c, i) => {
              const direction = sort?.column === i ? sort.direction : null
              // A declared label wins; otherwise the SQL alias, made readable.
              // Renaming here never touches the alias, so a cross-filter or a
              // column lookup still matches.
              const label = seriesLabel(c, panel.column_formats)
              // The button shows what a click will do. An unsorted column
              // sorts ascending first; a sorted one flips.
              const next = direction === "asc" ? "desc" : "asc"
              const NextIcon = next === "asc" ? ArrowDownIcon : ArrowUpIcon
              const nextText =
                next === "asc" ? "Sort ascending" : "Sort descending"
              return (
                <th
                  key={c}
                  aria-sort={
                    direction === "asc"
                      ? "ascending"
                      : direction === "desc"
                        ? "descending"
                        : "none"
                  }
                  className="group/sort border-b border-border/70 px-3 py-1.5 font-semibold text-muted-foreground"
                >
                  <div
                    className={cn(
                      "flex items-center gap-1 whitespace-nowrap",
                      // Numbers align right, so the arrow leads and the label
                      // stays flush with the figures beneath it.
                      numeric[i] ? "flex-row-reverse" : ""
                    )}
                  >
                    <span className={direction ? "text-foreground" : ""}>
                      {label}
                    </span>
                    <HintTooltip>
                      <HintTooltipTrigger asChild>
                        <button
                          type="button"
                          onClick={() => applySort({ column: i, direction: next })}
                          aria-label={`${nextText}: ${label}`}
                          className={cn(
                            "inline-flex size-5 shrink-0 items-center justify-center rounded transition hover:bg-muted hover:text-foreground focus-visible:opacity-100",
                            // The sorted column keeps its arrow in view so the
                            // table says how it is ordered; the rest appear on
                            // hover.
                            direction
                              ? "bg-muted text-foreground"
                              : "opacity-0 group-hover/sort:opacity-100"
                          )}
                        >
                          <NextIcon aria-hidden className="size-3" />
                        </button>
                      </HintTooltipTrigger>
                      <HintTooltipContent>{nextText}</HintTooltipContent>
                    </HintTooltip>
                  </div>
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {ordered.slice(0, shown).map((row, r) => (
            <tr
              key={r}
              className="odd:bg-muted/20 hover:bg-muted/40"
            >
              {row.map((cell, c) => (
                <td
                  key={c}
                  className={cn(
                    "border-b border-border/40 px-3 py-1.5 whitespace-nowrap",
                    numeric[c] ? "text-right tabular-nums" : "text-left"
                  )}
                >
                  {formatCell(
                    columns[c],
                    cell,
                    panel.column_formats,
                    dateGrains[c],
                    panel.default_currency
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {hasMore ? (
        <div
          ref={endRef}
          className="sticky left-0 flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground"
        >
          <span>
            Showing {shown.toLocaleString("en-US")} of{" "}
            {rows.length.toLocaleString("en-US")} rows
          </span>
          <button
            type="button"
            className="font-medium text-foreground underline-offset-2 hover:underline"
            onClick={() => setLimit((current) => current + TABLE_PAGE)}
          >
            Load more
          </button>
        </div>
      ) : null}
    </div>
  )
}

const AXIS = {
  tick: { fontSize: 12, fill: "var(--muted-foreground)" },
  axisLine: false,
  tickLine: false,
} as const

/** Let the value axis take the width its labels actually need.
 *
 *  Fixed at 56px, a money label wrapped onto two lines — "AED" sitting above
 *  "1.0M" — because the currency code costs width a bare "$" does not.
 *  Recharts 3.8 sizes the axis to its widest tick when given "auto". */
const VALUE_AXIS_WIDTH = "auto" as const

/** Approximate rendered width of one character at 12px, in px. */
const CHAR_PX = 6.4
/** Left gutter reserved for the value axis. */
const VALUE_AXIS_PX = 56
/** Horizontal room one label needs when rotated to -35°. */
const ROTATED_SLOT_PX = 15
/** sin(35°) — how much of a rotated label's length becomes height. */
const ROTATED_RISE = 0.574
/** Ceiling on the rotated-label gutter, so labels cannot eat the whole panel. */
const MAX_TICK_HEIGHT = 120

const CHART_HEIGHT = 280

/**
 * A panel's chart height after its row_span is applied.
 *
 * Recharts needs a definite pixel height, so the wrapper cannot squeeze a chart
 * the way it can a block of text — the height has to be computed here or a
 * shorter step does nothing at all. The floor keeps a squashed chart legible
 * rather than letting it collapse to an axis.
 */
function scaledHeight(base: number, panel: DashboardPanel): number {
  return Math.max(140, Math.round(base * rowSpanScale(panel)))
}

/**
 * The panel's own width, measured.
 *
 * The layout below needs to know how much room the axis actually has, and that
 * is not knowable from the data. Deciding blind is what left the old version
 * right only *sometimes*: the same eight categories fit comfortably in a
 * full-width panel and collide in a half-width one, and no rule written over
 * label lengths alone can tell those apart.
 *
 * State is set only from the observer callback — it fires once on observe with
 * the initial size, so an extra setState in the effect body would buy nothing
 * but a cascading render.
 */
function useMeasuredWidth<T extends HTMLElement>() {
  const ref = React.useRef<T | null>(null)
  const [width, setWidth] = React.useState(0)

  React.useLayoutEffect(() => {
    const element = ref.current
    if (!element) return

    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width ?? 0
      // Ignore sub-pixel jitter, or a resize turns into a render loop.
      setWidth((prev) => (Math.abs(prev - next) > 1 ? next : prev))
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return [ref, width] as const
}

/**
 * Cut a label to what fits, with an ellipsis so the truncation is visible.
 *
 * Measured, not estimated. The layout decides with measured widths, and a cut
 * made by an average character width disagreed with it: "Beauty & Personal
 * Care" measured to fit its rotated gutter, then lost its last word to an
 * estimate that thought 22 characters too many.
 */
function truncateToPx(text: string, maxPx: number): string {
  if (tickLabelPx(text) <= maxPx) return text
  let end = text.length - 1
  while (end > 2 && tickLabelPx(`${text.slice(0, end)}…`) > maxPx) end--
  return `${text.slice(0, end)}…`
}

/** Line height of a label wrapped over lines, and the most lines it may take. */
const TICK_LINE_PX = 14
const MAX_TICK_LINES = 3
/** Clear space kept between neighbouring labels. */
const TICK_GAP_PX = 10
/** A label that lies flat on its tick occupies this much height. */
const FLAT_TICK_HEIGHT = 28

/**
 * A label broken at its spaces into lines no wider than `maxPx`, or null when
 * it cannot be: a single word wider than the room, or more lines than allowed.
 * Words are never split mid-word — "Electro-nics" reads worse than a rotated
 * "Electronics", so a word that will not fit sends the axis to the next layout.
 */
function wrapTickLabel(
  text: string,
  maxPx: number,
  maxLines: number
): string[] | null {
  const words = text.split(/\s+/).filter(Boolean)
  if (words.length === 0) return [text]
  const lines: string[] = []
  let line = ""
  for (const word of words) {
    if (tickLabelPx(word) > maxPx) return null
    const candidate = line ? `${line} ${word}` : word
    if (tickLabelPx(candidate) <= maxPx) {
      line = candidate
    } else {
      lines.push(line)
      line = word
    }
  }
  lines.push(line)
  return lines.length <= maxLines ? lines : null
}

/**
 * How to lay a category axis out, decided from the labels AND the width.
 *
 * Recharts drops ticks it cannot fit and says nothing. On a chart of stores
 * that left "Circuit Electronics" captioned, the next bar blank, then "Crescent
 * Mall Flagship" — a reader cannot tell which bar is which. So nothing here is
 * left to that default, and the two kinds of axis are treated differently:
 *
 * A DATE SEQUENCE (weeks, months, years) reads left to right, and captioning
 * every third week is the convention, not a failure. Its labels lie flat and,
 * when they do not all fit, every Nth is shown at an even interval chosen here.
 *
 * NAMED CATEGORIES (stores, categories, markets) are each a thing the reader
 * is looking for, and none may go missing. In order of preference:
 *
 *   1. flat on one line — whenever they fit
 *   2. wrapped onto two lines, one word-group under another
 *   3. laid out horizontally, one label per row (bars only) — when names are
 *      long and numerous, where a row per label reads best
 *   4. wrapped onto three lines
 *   5. rotated to -35°, or upright at -90° when the slots are too narrow even
 *      for that — every label still shown
 *
 * A label that still cannot fit its room is cut with an ellipsis rather than
 * left to overlap: a clipped label reads as a fault, an elided one as an
 * elision, and the full name is in the tooltip either way.
 */
function axisPlan(
  labels: string[],
  forceHorizontal: boolean,
  seriesCount: number,
  containerWidth: number,
  {
    allowHorizontal = true,
    sequence = false,
  }: {
    allowHorizontal?: boolean
    /** The axis is a run of dates, which may be thinned; names never are. */
    sequence?: boolean
  } = {}
) {
  const count = labels.length || 1
  const longestPx = labels.reduce(
    (max, label) => Math.max(max, tickLabelPx(label)),
    0
  )
  const longestChars = labels.reduce((max, l) => Math.max(max, l.length), 0)

  // Width the category axis has to play with. Zero until the first
  // measurement lands; the fallbacks below keep that first paint sensible.
  const plotWidth =
    containerWidth > 0 ? Math.max(120, containerWidth - VALUE_AXIS_PX - 24) : 0
  const slot = plotWidth > 0 ? plotWidth / count : 0

  const fitsFlat =
    slot > 0
      ? longestPx + TICK_GAP_PX <= slot
      : count <= 6 && longestChars <= 8
  const fitsRotated = slot > 0 ? slot >= ROTATED_SLOT_PX : count <= 20

  const base = {
    horizontal: false as boolean,
    angled: false,
    angle: 0,
    interval: 0 as number | "preserveStartEnd",
    height: CHART_HEIGHT,
    categoryWidth: undefined as number | undefined,
    tickHeight: FLAT_TICK_HEIGHT as number | undefined,
    maxLabelPx: slot > 0 ? slot - TICK_GAP_PX : longestPx,
    lines: 1,
  }

  const horizontalPlan = () => {
    // One row per category, plus room for grouped series within each.
    const rowHeight = seriesCount > 1 ? Math.min(18 + seriesCount * 10, 46) : 26
    const categoryWidth = Math.min(
      200,
      Math.max(80, Math.round(longestPx) + 14)
    )
    return {
      ...base,
      horizontal: true,
      height: Math.min(640, Math.max(220, count * rowHeight + 64)),
      categoryWidth,
      tickHeight: undefined,
      // Leave room for the gap between label and axis.
      maxLabelPx: categoryWidth - 12,
    }
  }

  if (allowHorizontal && forceHorizontal) return horizontalPlan()

  if (sequence) {
    if (fitsFlat) return base
    // Every Nth date, flat, at an interval that gives each shown label the
    // room of the slots it spans. Before the first measurement, let the ends
    // anchor the axis rather than print every label over the next.
    const interval =
      slot > 0
        ? Math.max(1, Math.ceil((longestPx + TICK_GAP_PX) / slot) - 1)
        : ("preserveStartEnd" as const)
    return { ...base, interval, maxLabelPx: Math.ceil(longestPx) }
  }

  if (fitsFlat) return base

  const wrapRoom = slot - TICK_GAP_PX
  const wrapsInto = (lines: number) =>
    slot > 0 && labels.every((label) => wrapTickLabel(label, wrapRoom, lines))
  const wrappedPlan = (lines: number) => {
    const tickHeight = 12 + lines * TICK_LINE_PX
    return {
      ...base,
      tickHeight,
      height: CHART_HEIGHT + (tickHeight - FLAT_TICK_HEIGHT),
      maxLabelPx: wrapRoom,
      lines,
    }
  }

  if (wrapsInto(2)) return wrappedPlan(2)

  // Long AND numerous is the case a row-per-label serves best — twenty names
  // stacked three lines deep are legible one at a time and unreadable as a
  // group.
  const preferHorizontal = count > 6 && longestChars > 12
  if (allowHorizontal && (preferHorizontal || !fitsRotated)) {
    return horizontalPlan()
  }

  if (wrapsInto(MAX_TICK_LINES)) return wrappedPlan(MAX_TICK_LINES)

  // Rotated, every label shown. Too narrow for -35° — where each label needs
  // ROTATED_SLOT_PX across — they stand upright instead, which needs only a
  // line's height per label. Gutter and allowance come from ONE number so
  // they cannot disagree about the longest label.
  const upright = !fitsRotated
  const rise = upright ? 1 : ROTATED_RISE
  const rotatedLabelPx = Math.min(longestPx, (MAX_TICK_HEIGHT - 26) / rise)
  const tickHeight = Math.ceil(rotatedLabelPx * rise) + 26
  return {
    ...base,
    angled: true,
    angle: upright ? -90 : -35,
    tickHeight,
    height: CHART_HEIGHT + (tickHeight - FLAT_TICK_HEIGHT),
    // A rotated label's length is bounded by the gutter it rises through.
    maxLabelPx: Math.ceil(rotatedLabelPx),
  }
}

/* Every series opts OUT of Recharts' mount animation.
 *
 * The animation works by growing a clip rectangle from zero. A chart that
 * mounts off-screen — which is most of them on a dashboard long enough to
 * scroll — never runs it, so the clip stays collapsed and the panel renders
 * as axes and a legend around an empty plot. The data was always there; it
 * was being clipped away. The funnel had already been opted out for this
 * reason; the rest had not.
 */

/** Stroke weight for a plotted line. 2px reads as a hairline once a chart is
 *  scaled down into a dashboard cell. */
const LINE_STROKE = 2.5

/** The line drawn OVER bars in a combo panel. Heavier again, because it has to
 *  stay legible crossing filled shapes rather than empty plot area. */
const TREND_STROKE = 3.25

/** Headroom left above and below fitted data, as a share of its own span. */
const FIT_PAD = 0.08

/**
 * The value-axis domain for a panel that has opted out of a zero baseline.
 *
 * Returns undefined — meaning "pass no domain, let Recharts do what it always
 * did" — whenever the panel has not opted out, or the data yields no usable
 * extent. That is what keeps every existing dashboard rendering unchanged:
 * absent `zero_baseline` never reaches this function's padding logic.
 *
 * No `allowDataOverflow` goes with this. That flag clips the plot to the
 * domain, and since the domain is derived FROM the data nothing can overflow
 * it — but the clip it installs dropped every line in the chart, leaving axes
 * and a legend around an empty plot.
 *
 * Padding is a share of the data's OWN span, not of its maximum. A series
 * running 995-1000 needs half a unit of headroom; one running 0-1000 needs
 * eighty. Scaling by the maximum gives both the same, which re-flattens
 * exactly the trend the option exists to reveal.
 */
function fittedDomain(
  panel: DashboardPanel,
  rows: Array<Record<string, unknown>>,
  keys: string[]
): [number, number] | undefined {
  if (panel.zero_baseline !== false) return undefined

  let lo = Infinity
  let hi = -Infinity
  for (const row of rows) {
    for (const key of keys) {
      const value = Number(row[key])
      if (!Number.isFinite(value)) continue
      if (value < lo) lo = value
      if (value > hi) hi = value
    }
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return undefined

  // A flat series has no span to take a share of, so fall back to a share of
  // the value itself — and to 1 when the value is zero, which would otherwise
  // produce a zero-height domain Recharts cannot draw.
  const span = hi - lo
  const pad = span > 0 ? span * FIT_PAD : Math.max(Math.abs(hi) * 0.1, 1)
  return [lo - pad, hi + pad]
}

/**
 * Room the leftmost rotated label needs to the LEFT of the tick it belongs to.
 *
 * A -35° label is anchored at its right end, so it runs back and down-left
 * from its tick — and the first category's tick sits at the plot's left edge.
 * Recharts clips whatever crosses the chart's own margin, so a first category
 * of "Grocery & FMCG" arrived on screen as "& FMCG". The value axis absorbs
 * part of that run; AXIS_ALLOWANCE_PX is the part of it that is always there,
 * a deliberate under-estimate — spare margin costs a few pixels of plot, a
 * missing one costs the label.
 *
 * The same allowance sizes the room a flat label needs when it is centred on
 * the plot's edge — see axisLeftMargin.
 */
const ANGLED_RUN = 0.819 // cos 35°: the horizontal part of a rotated label
const AXIS_ALLOWANCE_PX = 16
const ANGLED_LABEL_PAD_PX = 8
const ANGLED_LEFT_MAX_PX = 96

/**
 * How wide a tick label actually draws, at the axis's 12px type.
 *
 * CHAR_PX is an average, and an average is right for the layout decisions it
 * was written for — a few percent out only nudges a threshold. Here the number
 * decides whether a label is visible at all, and the average under-measures
 * exactly the labels that need the room most: capitals, "&", wide names. So
 * this measures the string itself, and falls back to the average wherever
 * there is no canvas to measure with.
 */
let tickMetrics: CanvasRenderingContext2D | null | undefined
function tickLabelPx(text: string): number {
  if (tickMetrics === undefined) {
    const context =
      typeof document === "undefined"
        ? null
        : document.createElement("canvas").getContext("2d")
    if (context) {
      const family =
        getComputedStyle(document.body).fontFamily || "system-ui, sans-serif"
      context.font = `12px ${family}`
    }
    tickMetrics = context
  }
  return tickMetrics
    ? tickMetrics.measureText(text).width
    : text.length * CHAR_PX
}

/**
 * The chart's left margin, wide enough for the first axis label.
 *
 * Two layouts put that label partly left of the plot. A slanted label runs back
 * from its tick. And on a line or area chart the first point sits ON the plot's
 * left edge, so a flat or wrapped label centred under it hangs half outside —
 * "02-03-25" on a weekly refund chart lost its first digits. Bars and combos
 * centre their first label inside a band and need no extra room.
 */
function axisLeftMargin(
  plan: ReturnType<typeof axisPlan>,
  categories: readonly string[],
  { centredOnEdge = false }: { centredOnEdge?: boolean } = {}
): number {
  if (plan.horizontal || plan.angle === -90) return 8
  if (!plan.angled) {
    if (!centredOnEdge) return 8
    const first = String(categories[0] ?? "")
    const lines =
      plan.lines > 1
        ? (wrapTickLabel(first, plan.maxLabelPx, plan.lines) ?? [first])
        : [truncateToPx(first, plan.maxLabelPx)]
    const half = Math.ceil(
      lines.reduce((max, line) => Math.max(max, tickLabelPx(line)), 0) / 2
    )
    return Math.min(
      ANGLED_LEFT_MAX_PX,
      Math.max(8, half + ANGLED_LABEL_PAD_PX - AXIS_ALLOWANCE_PX)
    )
  }
  // The label as the axis will draw it — truncation included, since a label
  // cut to fit needs room for what is left of it, not for the whole name.
  const first = truncateToPx(String(categories[0] ?? ""), plan.maxLabelPx)
  const run =
    Math.ceil(tickLabelPx(first) * ANGLED_RUN) +
    ANGLED_LABEL_PAD_PX -
    AXIS_ALLOWANCE_PX
  return Math.min(ANGLED_LEFT_MAX_PX, Math.max(8, run))
}

type TickRenderProps = {
  x?: number | string
  y?: number | string
  payload?: { value?: unknown }
}

/**
 * One category label set over several lines, centred on its tick.
 *
 * Recharts draws a tick as a single line of text; given a custom tick it hands
 * over the position and value and draws nothing itself, so the lines are laid
 * out here with the same wrapping the plan used to size the axis.
 */
function WrappedTick({
  x,
  y,
  payload,
  label,
  maxPx,
  maxLines,
}: TickRenderProps & {
  label: (value: unknown) => string
  maxPx: number
  maxLines: number
}) {
  const text = label(payload?.value)
  const lines = wrapTickLabel(text, maxPx, maxLines) ?? [
    truncateToPx(text, maxPx),
  ]
  const cx = Number(x ?? 0)
  return (
    <text
      x={cx}
      y={Number(y ?? 0)}
      textAnchor="middle"
      fontSize={12}
      fill="var(--muted-foreground)"
    >
      {lines.map((line, i) => (
        <tspan key={i} x={cx} dy={i === 0 ? "0.71em" : TICK_LINE_PX}>
          {line}
        </tspan>
      ))}
    </text>
  )
}

/**
 * Axis props that keep every category's label, or thin a date axis evenly.
 * `label` is the uncut label: wrapped ticks break it into lines themselves,
 * where cutting it to one line's width first would lose the words that belong
 * on the second.
 */
function categoryTickProps(
  plan: ReturnType<typeof axisPlan>,
  label: (value: unknown) => string
) {
  if (plan.angled) {
    return {
      interval: plan.interval,
      angle: plan.angle,
      textAnchor: "end" as const,
      height: plan.tickHeight,
    }
  }
  if (plan.lines > 1) {
    return {
      interval: plan.interval,
      height: plan.tickHeight,
      tick: (props: TickRenderProps) => (
        <WrappedTick
          {...props}
          label={label}
          maxPx={plan.maxLabelPx}
          maxLines={plan.lines}
        />
      ),
    }
  }
  return { interval: plan.interval, height: plan.tickHeight }
}

/* --------------------------------------------------- the chart standard */

/**
 * A value axis's range and ticks, under the chart standard.
 *
 * Stacks reach from zero to their tallest total, and a stack of shares stops
 * its ticks at 100. Anything else includes zero unless the panel opted out,
 * in which case it fits the data with a little room either side. Every result
 * is rounded to nice ticks, which is what ended axes reading 13.9M / 18.4M.
 *
 * "Includes zero" works in both directions. Recharts' own default range is
 * [0, 'auto'], and when every value is negative 'auto' resolves below zero,
 * zero drops out, and bars are measured from the least negative value — the
 * smallest had no length at all, and a chart of points below target lost its
 * three best performers.
 */
function standardScale(
  panel: DashboardPanel,
  rows: ReadonlyArray<Record<string, unknown>>,
  keys: readonly string[],
  { stacked = false }: { stacked?: boolean } = {}
): Scale | undefined {
  if (stacked) {
    const totals = stackTotals(rows, keys)
    if (!totals) return undefined
    return (
      shareStackScale(totals) ??
      niceScale(totals[0], totals[1], { includeZero: true })
    )
  }
  const range = extent(rows, keys)
  if (!range) return undefined
  return panel.zero_baseline === false
    ? niceScale(range[0], range[1], { pad: 0.06 })
    : niceScale(range[0], range[1], { includeZero: true })
}

type AxisText = {
  fmt: AxisFormat
  title: string | undefined
  tick: (value: unknown) => string
}

/**
 * How a value axis writes its title and ticks: one unit for every tick, the
 * currency named once in the title rather than on each tick, and the panel's
 * own settings winning over the standard.
 */
function useAxisText(
  panel: DashboardPanel,
  keys: readonly string[],
  scale: Scale | undefined,
  /* Which title setting this axis takes. "none" for an axis no setting names,
     such as a combo's right-hand axis, which keeps its standard title. */
  { setting = "y" }: { setting?: "x" | "y" | "none" } = {}
): AxisText {
  const { currencyUnit } = useChartDisplay()
  const fmt = axisFormat({
    name: keys[0],
    formats: panel.column_formats,
    defaultCurrency: panel.default_currency,
    scale,
    unit: panel.format?.unit,
    sharedUnit: currencyUnit,
  })
  const own =
    setting === "x"
      ? panel.format?.x_title
      : setting === "y"
        ? panel.format?.y_title
        : undefined
  const title = titleOr(
    own,
    valueAxisTitle(keys, fmt, panel.column_formats, panel.columns)
  )
  const currencyInTitle = titleCarriesCurrency(fmt, title)
  return {
    fmt,
    title,
    tick: (value) =>
      typeof value === "number"
        ? formatTick(value, fmt, { currencyInTitle })
        : String(value ?? ""),
  }
}

/** The title for the axis carrying a panel's category or date column. */
function categoryTitle(
  panel: DashboardPanel,
  column: string | null | undefined,
  categories: readonly unknown[]
): string | undefined {
  const standard =
    dateAxisTitle(categories) ??
    (column
      ? seriesLabel(column, panel.column_formats, panel.columns)
      : undefined)
  return titleOr(panel.format?.x_title, standard)
}

/** The title over a chart's vertical axis — and, for a combo, its right one. */
function AxisTitles({ left, right }: { left?: string; right?: string }) {
  if (!left && !right) return null
  return (
    <div className="mb-1 flex min-w-0 items-baseline justify-between gap-3 px-1 text-[11px] font-medium text-muted-foreground">
      <span className="min-w-0 truncate">{left}</span>
      {right ? (
        <span className="min-w-0 truncate text-right">{right}</span>
      ) : null}
    </div>
  )
}

/** Room a horizontal axis's title takes under its ticks. */
const X_TITLE_PX = 18

/** Props that put a title under a horizontal axis's ticks. */
function bottomTitle(title: string | undefined, height: number | undefined) {
  if (!title) return height === undefined ? {} : { height }
  return {
    height: (height ?? 30) + X_TITLE_PX,
    label: {
      value: title,
      position: "insideBottom" as const,
      offset: 0,
      fontSize: 11,
      fontWeight: 500,
      fill: "var(--muted-foreground)",
    },
  }
}

/**
 * The legend a chart shows, in the order its series are drawn.
 *
 * Recharts sorts legend items alphabetically unless told otherwise, so a
 * stacked bar read BNPL, Card, Cash, Digital Wallet while its segments and
 * tooltip ran Cash, Card, Digital Wallet, BNPL. A stack lists its top segment
 * first, matching the order a reader meets them going down the bar.
 */
function legendNode(
  position: LegendPosition,
  keys: readonly string[],
  { reverse = false }: { reverse?: boolean } = {}
) {
  if (position === "none") return null
  const itemSorter = ((item: { dataKey?: unknown; value?: unknown }) => {
    const index = keys.indexOf(String(item.dataKey ?? item.value ?? ""))
    return reverse ? -index : index
  }) as unknown as "value"
  return position === "right" ? (
    <Legend
      layout="vertical"
      align="right"
      verticalAlign="middle"
      iconType="square"
      itemSorter={itemSorter}
      wrapperStyle={{ fontSize: 12, paddingLeft: 12 }}
    />
  ) : (
    <Legend
      iconType="square"
      itemSorter={itemSorter}
      wrapperStyle={{ fontSize: 12, paddingTop: 8 }}
    />
  )
}

/** Extra chart height a legend under the plot needs. */
function legendRoom(position: LegendPosition): number {
  return position === "bottom" ? 24 : 0
}

function ChartTooltipBox({
  active,
  payload,
  label,
  dateGrain,
  formats,
  defaultCurrency,
  reverse = false,
}: {
  active?: boolean
  payload?: Array<{
    name?: string
    value?: unknown
    color?: string
    /* The series' real column. `name` is only what is displayed, and may be a
       readable label ("Net Revenue"); formats are keyed on the column. */
    dataKey?: string | number
  }>
  label?: unknown
  /* Passed by panels whose x axis is a date, so the tooltip heading reads
     "Mar 2025" like the axis under it rather than "2025-03-01". */
  dateGrain?: DateGranularity | null
  /* The tooltip formats the same numbers the axis and the table do, so it
     needs the same two inputs — without them a hover showed dollars over a
     chart whose axis said AED. */
  formats?: ColumnFormats | null
  defaultCurrency?: string | null
  /* A stack lists its top segment first, as its legend does. */
  reverse?: boolean
}) {
  if (!active || !payload?.length) return null
  const entries = reverse ? [...payload].reverse() : payload
  return (
    <div className="rounded-md border border-border/80 bg-popover px-2.5 py-2 text-xs shadow-md">
      {label !== undefined && label !== null && String(label) !== "" ? (
        <p className="mb-1.5 font-medium text-foreground">
          {/* Not compact: an axis can leave the year off a tick, but the
              heading of the one point being inspected should say which year. */}
          {formatDateLabel(label, dateGrain ?? null, false)}
        </p>
      ) : null}
      {entries.map((entry, i) => (
        <p key={i} className="flex items-center gap-1.5 tabular-nums">
          <span
            className="size-2 shrink-0 rounded-[2px]"
            style={{ background: entry.color }}
          />
          <span className="min-w-0 truncate text-muted-foreground">
            {entry.name}
          </span>
          <span className="ml-auto font-medium text-foreground">
            {/* Full cell precision on hover — axes stay compact. Looked up by
                the column, never the displayed name: a readable label would
                match no format, and AED values would lose their currency. */}
            {formatCell(
              entry.dataKey !== undefined ? String(entry.dataKey) : entry.name,
              entry.value,
              formats,
              null,
              defaultCurrency
            )}
          </span>
        </p>
      ))}
    </div>
  )
}

function BarPanel({
  panel,
  onCategoryClick,
  selectedValue,
}: {
  panel: DashboardPanel
  onCategoryClick?: (value: string) => void
  selectedValue?: string | null
}) {
  const { columns, rows, x, color, orientation } = panel
  /* Four spellings, three meanings.

     null and "v" both mean AUTO: upright, but free to flip to horizontal rows
     once the category labels stop fitting. "v" is in that group deliberately —
     the planner has emitted it for a long time (see the orientation line in
     llm.py's dashboard schema) and 35 saved panels carry it, so giving it a new
     meaning would silently re-draw charts nobody asked us to touch.

     "vertical" is the explicit lock: stay upright even when the labels are
     awkward. It is what the editor writes, so a panel only stops auto-flipping
     when a person says so.

     "h" forces horizontal, and always has. */
  const horizontal = orientation === "h" || orientation === "horizontal"
  const lockVertical = orientation === "vertical"
  const yFields = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []
  const stacked = panel.chart_type === "stacked_bar"

  // Which axis holds the value is inferred from the DATA, not from the x/y
  // roles: a horizontal bar names its category in `y` about as often as in `x`.
  let category = x
  let values = yFields
  if (
    !stacked &&
    isNumericColumn(rows, columnIndex(columns, x)) &&
    yFields.length === 1 &&
    !isNumericColumn(rows, columnIndex(columns, yFields[0]))
  ) {
    category = yFields[0]
    values = [x as string]
  }

  const data = toChartData(columns, rows)
  const grouped = Boolean(color) && !stacked

  // A colour column splits one value column into a series per distinct value.
  const seriesKeys = grouped
    ? Array.from(
        new Set(rows.map((r) => String(r[columnIndex(columns, color)])))
      )
    : values

  const shaped = grouped
    ? Array.from(new Set(data.map((d) => String(d[category as string])))).map(
        (catValue) => {
          const item: Record<string, unknown> = {
            [category as string]: catValue,
          }
          for (const key of seriesKeys) {
            const match = data.find(
              (d) =>
                String(d[category as string]) === catValue &&
                String(d[color as string]) === key
            )
            item[key] = match ? match[values[0]] : null
          }
          return item
        }
      )
    : data

  const valueName = values[0]

  const categories = shaped.map((d) => String(d[category as string] ?? ""))
  // A month or year series is stored as the first day of its period, so the
  // raw label reads "2025-01-01". Shorten AFTER converting, or the truncation
  // budget is spent on a day number that carries no information.
  const dateGrain = detectDateGranularity(categories)
  const acrossYears = spansYears(categories)
  const axisLabel = (v: unknown) =>
    formatDateLabel(v, dateGrain, true, acrossYears)
  const [sizeRef, width] = useMeasuredWidth<HTMLDivElement>()
  const plan = axisPlan(
    categories.map(axisLabel),
    horizontal,
    seriesKeys.length,
    width,
    { allowHorizontal: !lockVertical, sequence: dateGrain !== null }
  )

  /* Stacked bars never lift off the baseline: their segments only read as
     parts of a whole because the stack starts at zero. Lifted off it the
     lengths stop summing to anything, so the option is neither offered in the
     editor nor honoured here. */
  /* Stacked bars never lift off the baseline — standardScale ignores the
     opt-out for them — and a stack of shares stops its ticks at 100. */
  const scale = standardScale(panel, shaped, seriesKeys, { stacked })
  // The unit comes from the measure. The title names it only when there is
  // one: a stack of four shares titled after its first ("Cash %") mislabels
  // the other three, and the legend already names them.
  const valueText = useAxisText(panel, grouped ? [valueName] : values, scale)
  const categoryAxisTitle = categoryTitle(panel, category, categories)
  const legend = legendFor(seriesKeys.length, panel.format?.legend)
  // A series split out of a colour column is named by a value, not a column,
  // so settings kept by column cannot apply to it.
  const overridable = !grouped
  const labelsOf = (key: string) =>
    seriesLabels(panel.chart_type, shaped.length, panel.format, key, {
      overridable,
    })
  const anyLabels = seriesKeys.some((key) => labelsOf(key) !== "none")
  const totals = stacked && panel.format?.stack_totals === true
  const maxBarSize = plan.horizontal ? 22 : 40
  // Room for labels beyond the bars: above upright ones, right of lying ones.
  // A bar too narrow for its label carries it upright, which needs more.
  const roughBarPx =
    width > 0
      ? (((width - 80) / Math.max(1, shaped.length)) * 0.8) /
        Math.max(1, seriesKeys.length)
      : 40
  const labelTop = plan.horizontal
    ? 0
    : stacked
      ? totals
        ? LABEL_ROOM
        : 0
      : !anyLabels
        ? 0
        : roughBarPx < 36
          ? UPRIGHT_LABEL_ROOM
          : LABEL_ROOM
  const labelRight =
    plan.horizontal && (stacked ? totals : anyLabels)
      ? horizontalLabelRoom(
          shaped,
          seriesKeys,
          valueText.fmt,
          panel.format?.unit,
          titleCarriesCurrency(valueText.fmt, valueText.title)
        )
      : 0
  const shortenTick = (v: unknown) => truncateToPx(axisLabel(v), plan.maxLabelPx)

  const handleBarClick = (entry: unknown) => {
    if (!onCategoryClick || !category) return
    const payload =
      entry && typeof entry === "object" && "payload" in entry
        ? (entry as { payload?: Record<string, unknown> }).payload
        : (entry as Record<string, unknown> | undefined)
    const raw = payload?.[category as string]
    if (raw === null || raw === undefined) return
    onCategoryClick(String(raw))
  }

  // The vertical axis's title sits above the plot; the horizontal one's goes
  // under its ticks. Which is which swaps when the bars lie on their side.
  const verticalTitle = plan.horizontal ? categoryAxisTitle : valueText.title
  const horizontalTitle = plan.horizontal ? valueText.title : categoryAxisTitle

  return (
    <div ref={sizeRef} className="w-full min-w-0">
      <AxisTitles left={verticalTitle} />
      <ChartContainer
        config={{}}
        className="aspect-auto w-full"
        // An EXPLICIT pixel height, not flex-1 with a minimum. Recharts'
        // ResponsiveContainer measures its parent once at mount; under
        // `flex: 1 1 0%` that measurement happens before layout resolves, so
        // it reads zero and renders no chart at all. On a dashboard the grid
        // supplied a definite height and hid the bug; in chat, where the card
        // sits in normal flow, every chart came out blank.
        style={{
          height: scaledHeight(
            plan.height +
              legendRoom(legend) +
              (horizontalTitle ? X_TITLE_PX : 0) +
              labelTop,
            panel
          ),
        }}
      >
        <BarChart
          data={shaped}
          layout={plan.horizontal ? "vertical" : "horizontal"}
          margin={{
            top: 8 + labelTop,
            right: 16 + labelRight,
            bottom: legend === "bottom" ? 12 : 4,
            left: axisLeftMargin(plan, categories.map(axisLabel)),
          }}
        >
          <CartesianGrid
            strokeDasharray="3 3"
            vertical={plan.horizontal}
            horizontal={!plan.horizontal}
            stroke="var(--border)"
          />
          {plan.horizontal ? (
            <>
              <XAxis
                type="number"
                {...AXIS}
                domain={scale?.domain}
                ticks={scale?.ticks}
                tickFormatter={valueText.tick}
                {...bottomTitle(horizontalTitle, undefined)}
              />
              <YAxis
                type="category"
                dataKey={category as string}
                width={plan.categoryWidth}
                interval={0}
                tickFormatter={shortenTick}
                {...AXIS}
              />
            </>
          ) : (
            <>
              <XAxis
                dataKey={category as string}
                tickFormatter={shortenTick}
                {...AXIS}
                {...categoryTickProps(plan, axisLabel)}
                {...bottomTitle(horizontalTitle, plan.tickHeight)}
              />
              <YAxis
                {...AXIS}
                width={VALUE_AXIS_WIDTH}
                domain={scale?.domain}
                ticks={scale?.ticks}
                tickFormatter={valueText.tick}
              />
            </>
          )}
          <Tooltip
            content={
              <ChartTooltipBox
                formats={panel.column_formats}
                defaultCurrency={panel.default_currency}
                dateGrain={dateGrain}
                reverse={stacked}
              />
            }
            cursor={{ opacity: 0.1 }}
          />
          {legendNode(legend, seriesKeys, { reverse: stacked })}
          {seriesKeys.map((key, i) => (
            <Bar
            isAnimationActive={false}
              key={key}
              dataKey={key}
              name={seriesLabel(key, panel.column_formats, columns)}
              stackId={stacked ? "stack" : undefined}
              fill={seriesColor(i, key, panel.palette)}
              radius={
                stacked ? 0 : plan.horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]
              }
              maxBarSize={maxBarSize}
              cursor={onCategoryClick ? "pointer" : undefined}
              onClick={handleBarClick}
            >
              {shaped.map((d, idx) => {
                const cat = String(d[category as string] ?? "")
                const dim =
                  Boolean(selectedValue) && cat !== selectedValue
                return (
                  <Cell
                    key={idx}
                    fill={seriesColor(i, key, panel.palette)}
                    fillOpacity={dim ? 0.28 : 1}
                  />
                )
              })}
            </Bar>
          ))}
          <ValueLabels
            data={shaped}
            categoryKey={category as string}
            unit={panel.format?.unit}
            horizontal={plan.horizontal}
            barSlots={stacked ? 1 : seriesKeys.length}
            maxBarSize={maxBarSize}
            valueDomain={scale?.domain}
            room={labelTop}
            stackTotals={
              totals
                ? {
                    fmt: valueText.fmt,
                    bare: titleCarriesCurrency(valueText.fmt, valueText.title),
                  }
                : undefined
            }
            series={seriesKeys.map((key, i) => ({
              key,
              color: seriesColor(i, key, panel.palette),
              fmt: valueText.fmt,
              shape: stacked ? "stack" : "bar",
              labels: labelsOf(key),
              position: seriesLabelPosition(panel.format, key, { overridable }),
              barSlot: i,
              bare: titleCarriesCurrency(valueText.fmt, valueText.title),
            }))}
          />
        </BarChart>
      </ChartContainer>
    </div>
  )
}

function LinePanel({ panel }: { panel: DashboardPanel }) {
  const { columns, rows, x, color } = panel
  const filled = panel.chart_type === "area"
  const yFields = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []
  const data = toChartData(columns, rows)

  const seriesKeys = color
    ? Array.from(
        new Set(rows.map((r) => String(r[columnIndex(columns, color)])))
      )
    : yFields

  const shaped = color
    ? Array.from(new Set(data.map((d) => String(d[x as string])))).map(
        (cat) => {
          const item: Record<string, unknown> = { [x as string]: cat }
          for (const key of seriesKeys) {
            const match = data.find(
              (d) => String(d[x as string]) === cat && String(d[color]) === key
            )
            item[key] = match ? match[yFields[0]] : null
          }
          return item
        }
      )
    : data

  const categories = shaped.map((d) => String(d[x as string] ?? ""))
  const [sizeRef, width] = useMeasuredWidth<HTMLDivElement>()
  // A line's x-axis order carries meaning, so it is never flipped to a
  // row-per-label layout. A run of dates that will not fit is thinned to every
  // Nth, the convention for a dense weekly series; a line across named
  // categories keeps every name, wrapped or rotated as the plan decides.
  const scale = standardScale(panel, shaped, seriesKeys)
  // Split by a colour column, the series are that column's values, so the
  // unit and the title come from the measure being split, not a value of it.
  const measures = color ? yFields.slice(0, 1) : seriesKeys
  const valueText = useAxisText(panel, measures, scale)

  const dateGrain = detectDateGranularity(categories)
  const acrossYears = spansYears(categories)
  const axisLabel = (v: unknown) =>
    formatDateLabel(v, dateGrain, true, acrossYears)
  const plan = axisPlan(
    categories.map(axisLabel),
    false,
    seriesKeys.length,
    width,
    { allowHorizontal: false, sequence: dateGrain !== null }
  )
  const legend = legendFor(seriesKeys.length, panel.format?.legend)
  const shortenTick = (v: unknown) => truncateToPx(axisLabel(v), plan.maxLabelPx)
  const xTitle = categoryTitle(panel, x, categories)

  // Series split out of a colour column are named by values, not columns, so
  // settings kept by column cannot apply to them.
  const overridable = !color
  const labelsOf = (key: string) =>
    seriesLabels(panel.chart_type, shaped.length, panel.format, key, {
      overridable,
    })
  const markersOf = (key: string) =>
    seriesMarkers(panel.format, key, shaped.length, { overridable })
  const labelTop = seriesKeys.some((key) => labelsOf(key) !== "none")
    ? LABEL_ROOM
    : 0

  const Chart = filled ? AreaChart : LineChart

  return (
    <div ref={sizeRef} className="w-full min-w-0">
      <AxisTitles left={valueText.title} />
      <ChartContainer
        config={{}}
        className="aspect-auto w-full"
        // An EXPLICIT pixel height, not flex-1 with a minimum. Recharts'
        // ResponsiveContainer measures its parent once at mount; under
        // `flex: 1 1 0%` that measurement happens before layout resolves, so
        // it reads zero and renders no chart at all. On a dashboard the grid
        // supplied a definite height and hid the bug; in chat, where the card
        // sits in normal flow, every chart came out blank.
        style={{
          height: scaledHeight(
            plan.height +
              legendRoom(legend) +
              (xTitle ? X_TITLE_PX : 0) +
              labelTop,
            panel
          ),
        }}
      >
        <Chart
          data={shaped}
          margin={{
            top: 8 + labelTop,
            right: 16,
            bottom: legend === "bottom" ? 12 : 4,
            left: axisLeftMargin(plan, categories.map(axisLabel), {
              centredOnEdge: true,
            }),
          }}
        >
          <CartesianGrid
            strokeDasharray="3 3"
            vertical={false}
            stroke="var(--border)"
          />
          <XAxis
            dataKey={x as string}
            tickFormatter={shortenTick}
            {...AXIS}
            {...categoryTickProps(plan, axisLabel)}
            {...bottomTitle(xTitle, plan.tickHeight)}
          />
          <YAxis
            {...AXIS}
            width={VALUE_AXIS_WIDTH}
            domain={scale?.domain}
            ticks={scale?.ticks}
            tickFormatter={valueText.tick}
          />
          <Tooltip
            content={
              <ChartTooltipBox
                formats={panel.column_formats}
                defaultCurrency={panel.default_currency}
                dateGrain={dateGrain}
              />
            }
          />
          {legendNode(legend, seriesKeys)}
          {seriesKeys.map((key, i) => {
            const colour = seriesColor(i, key, panel.palette)
            return filled ? (
              <Area
            isAnimationActive={false}
                key={key}
                type="monotone"
                dataKey={key}
                name={seriesLabel(key, panel.column_formats, columns)}
                stroke={colour}
                fill={colour}
                fillOpacity={seriesKeys.length > 1 ? 0.18 : 0.28}
                strokeWidth={LINE_STROKE}
                dot={markersOf(key) === "all" ? markerDot(colour) : false}
                activeDot={{ r: 5 }}
                connectNulls
              />
            ) : (
              <Line
            isAnimationActive={false}
                key={key}
                type="monotone"
                dataKey={key}
                name={seriesLabel(key, panel.column_formats, columns)}
                stroke={colour}
                strokeWidth={LINE_STROKE}
                /* Ringed in the series colour, never a bare white dot: with no
                   outline every point punched a hole through the line and a
                   long weekly series read as dashed. Past MARKER_LIMIT points
                   only labelled points are marked, by the label layer. */
                dot={markersOf(key) === "all" ? markerDot(colour) : false}
                activeDot={{ r: 5 }}
                connectNulls
              />
            )
          })}
          <ValueLabels
            data={shaped}
            categoryKey={x as string}
            unit={panel.format?.unit}
            series={seriesKeys.map((key, i) => ({
              key,
              color: seriesColor(i, key, panel.palette),
              fmt: valueText.fmt,
              shape: "point",
              labels: labelsOf(key),
              markers: markersOf(key),
              bare: titleCarriesCurrency(valueText.fmt, valueText.title),
            }))}
          />
        </Chart>
      </ChartContainer>
    </div>
  )
}

function PiePanel({
  panel,
  onCategoryClick,
  selectedValue,
}: {
  panel: DashboardPanel
  onCategoryClick?: (value: string) => void
  selectedValue?: string | null
}) {
  const { columns, rows, x } = panel
  const yFields = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []
  const valueKey = yFields[0]
  const data = toChartData(columns, rows)
  const total = rows.reduce((sum, r) => {
    const v = r[columnIndex(columns, valueKey)]
    return sum + (typeof v === "number" ? v : 0)
  }, 0)

  return (
    <div>
      {/* Legend above the chart with the share, so slices are readable
          without hovering — the donut itself carries no labels. Capped and
          scrollable: a high-cardinality slice column would otherwise push the
          donut itself out of the card. */}
      <div className="scrollbar-slim mb-2 flex max-h-24 flex-wrap gap-x-4 gap-y-1 overflow-y-auto overscroll-contain text-xs text-muted-foreground">
        {data.map((d, i) => {
          const value = d[valueKey]
          const label = String(d[x as string] ?? "")
          const pct =
            total && typeof value === "number"
              ? ` ${Math.round((value / total) * 100)}%`
              : ""
          const dim =
            Boolean(selectedValue) && label !== selectedValue
          return (
            <button
              key={i}
              type="button"
              className={cn(
                "flex items-center gap-1.5 rounded-sm text-left",
                onCategoryClick && "cursor-pointer hover:text-foreground",
                dim && "opacity-40"
              )}
              onClick={() => onCategoryClick?.(label)}
              disabled={!onCategoryClick}
            >
              <span
                className="size-2.5 rounded-[2px]"
                style={{
                  background: seriesColor(i, label, panel.palette),
                }}
              />
              {label}
              {pct}
            </button>
          )
        })}
      </div>
      <ChartContainer
        config={{}}
        className="aspect-auto w-full"
        style={{ height: scaledHeight(220, panel) }}
      >
        <PieChart>
          <Pie
            isAnimationActive={false}
            data={data}
            dataKey={valueKey}
            nameKey={x as string}
            innerRadius="55%"
            outerRadius="85%"
            paddingAngle={2}
            cursor={onCategoryClick ? "pointer" : undefined}
            onClick={(_, index) => {
              if (!onCategoryClick || index == null) return
              const d = data[index as number]
              if (!d) return
              onCategoryClick(String(d[x as string] ?? ""))
            }}
          >
            {data.map((d, i) => {
              const label = String(d[x as string] ?? "")
              const dim =
                Boolean(selectedValue) && label !== selectedValue
              return (
                <Cell
                  key={i}
                  fill={seriesColor(i, label, panel.palette)}
                  stroke="var(--background)"
                  fillOpacity={dim ? 0.28 : 1}
                />
              )
            })}
          </Pie>
          <Tooltip content={<ChartTooltipBox
              formats={panel.column_formats}
              defaultCurrency={panel.default_currency} />} />
        </PieChart>
      </ChartContainer>
    </div>
  )
}

type PointLabelProps = {
  x?: number | string
  y?: number | string
  width?: number | string
  height?: number | string
  value?: unknown
  index?: number
}

/**
 * One point's name, on a single line beside it.
 *
 * Recharts' own label wraps its text to the width of the dot it belongs to,
 * which broke "Fashion & Apparel" into three stacked words. A point in the
 * right-hand part of the plot is named on its left, or a long name runs off
 * the edge.
 */
function PointLabel({
  x,
  y,
  width,
  height,
  value,
  index,
  sides,
  shifts,
  gaps,
}: PointLabelProps & {
  sides: readonly boolean[]
  shifts: readonly number[]
  /** How far each label sits from its point's centre: past the edge of its bubble. */
  gaps?: readonly number[]
}) {
  const text = String(value ?? "")
  if (!text) return null
  const cx = Number(x) + Number(width ?? 0) / 2
  const cy = Number(y) + Number(height ?? 0) / 2
  if (!Number.isFinite(cx) || !Number.isFinite(cy)) return null
  const left = index !== undefined && sides[index] === true
  const shift = index !== undefined ? (shifts[index] ?? 0) : 0
  const gap = index !== undefined ? (gaps?.[index] ?? 9) : 9
  return (
    <text
      x={cx + (left ? -gap : gap)}
      y={cy + shift}
      textAnchor={left ? "end" : "start"}
      dominantBaseline="central"
      fontSize={11}
      fill="var(--muted-foreground)"
      stroke="var(--background)"
      strokeWidth={2.5}
      paintOrder="stroke"
    >
      {text}
    </text>
  )
}

/** Width the scatter's value axis and right margin take from the plot. */
const SCATTER_GUTTER_PX = 63

/** Values of one column that are real numbers. */
function numbersOf(
  rows: ReadonlyArray<Record<string, unknown>>,
  key: string | null | undefined
): number[] {
  if (!key) return []
  return rows.flatMap((row) => {
    const v = row[key]
    return typeof v === "number" && Number.isFinite(v) ? [v] : []
  })
}

/** Whether a value sits inside an axis range, or the axis has none. */
function withinDomain(
  value: unknown,
  domain: readonly [number, number] | undefined
): boolean {
  if (!domain || typeof value !== "number") return true
  const slack = (domain[1] - domain[0]) * 1e-9
  return value >= domain[0] - slack && value <= domain[1] + slack
}

/** A reference line's name, drawn at the end of the line with a halo. */
function ReferenceTag({
  viewBox,
  text,
  vertical,
}: {
  viewBox?: { x?: number; y?: number; width?: number; height?: number }
  text: string
  vertical: boolean
}) {
  if (!viewBox || viewBox.x === undefined || viewBox.y === undefined)
    return null
  const x = vertical ? viewBox.x + 5 : viewBox.x + (viewBox.width ?? 0) - 5
  const y = vertical ? viewBox.y + 10 : viewBox.y - 6
  return (
    <text
      x={x}
      y={y}
      textAnchor={vertical ? "start" : "end"}
      fontSize={11}
      fill="var(--muted-foreground)"
      stroke="var(--background)"
      strokeWidth={3}
      paintOrder="stroke"
    >
      {text}
    </text>
  )
}

const REFERENCE_NAMES = { mean: "Mean", median: "Median", value: "" } as const

function ScatterPanel({ panel }: { panel: DashboardPanel }) {
  const { columns, rows, x, color } = panel
  const format = panel.format
  const yFields = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []
  const yKey = yFields[0]
  const data = toChartData(columns, rows)
  // A size column only sizes bubbles when it holds numbers.
  const size =
    panel.size && isNumericColumn(rows, columnIndex(columns, panel.size))
      ? panel.size
      : null

  const groups = color
    ? Array.from(
        new Set(rows.map((r) => String(r[columnIndex(columns, color)])))
      )
    : [null]

  /* Reference lines sit at the mean or median of every point, or at a value
     typed in; the axes include them, so a typed value is never off the plot. */
  const xRef = referenceValue(numbersOf(data, x), format?.x_reference)
  const yRef = referenceValue(numbersOf(data, yKey), format?.y_reference)
  const withRef = (key: string, ref: number | undefined) =>
    ref === undefined ? data : [...data, { [key]: ref }]

  /* Both axes follow the panel's axis setting. Only the value axis used to:
     a scatter's x stayed at zero while y fitted the data, so eleven categories
     whose discounts all sat between 10.8% and 12.5% were squeezed into the
     right-hand quarter of the plot. A scatter's points are positions on both
     axes, not lengths, so neither axis has a reason to be treated differently.
     Bounds a person set replace the standard's ends. */
  const xScale = x
    ? boundedScale(standardScale(panel, withRef(x, xRef), [x]), {
        min: format?.x_min,
        max: format?.x_max,
      })
    : undefined
  const yScale = yKey
    ? boundedScale(standardScale(panel, withRef(yKey, yRef), [yKey]), {
        min: format?.y_min,
        max: format?.y_max,
      })
    : undefined
  const xText = useAxisText(panel, x ? [x] : [], xScale, { setting: "x" })
  const yText = useAxisText(panel, yKey ? [yKey] : [], yScale)

  /* Points outside manual bounds are left out and counted, rather than drawn
     over the axes or clipped without a word. */
  const shown = data.filter(
    (d) =>
      withinDomain(d[x as string], xScale?.domain) &&
      withinDomain(d[yKey], yScale?.domain)
  )
  const hidden = data.length - shown.length

  /* Points are named where they sit. Eleven categories identified only by a
     legend's colours — three of which had come out the same grey — could not
     be told apart at all. Named on the chart, the legend has nothing left to
     say, so the standard drops it unless the panel asks for one. */
  const labels = color
    ? dataLabelsFor(panel.chart_type, shown.length, format?.data_labels)
    : "none"
  // Highest and lowest are by the value axis.
  const named = new Set(
    labels === "all"
      ? shown.map((_, i) => i)
      : yKey
        ? labelIndices(
            shown.map((d) => d[yKey]),
            labels
          )
        : []
  )
  const labelled: Array<Record<string, unknown>> = shown.map((d, i) => ({
    ...d,
    __label: named.has(i) ? String(d[color as string] ?? "") : "",
  }))
  const legend = legendFor(groups.length, format?.legend, {
    labelledDirectly: labels !== "none",
  })
  // Which side of its point each label goes: the left, for a point in the
  // right-hand part of the x range.
  const [xLo, xHi] = xScale?.domain ?? [0, 1]
  const onLeft = (row: Record<string, unknown>) => {
    const value = row[x as string]
    return (
      typeof value === "number" &&
      xHi > xLo &&
      (value - xLo) / (xHi - xLo) > 0.7
    )
  }
  const [sizeRef, width] = useMeasuredWidth<HTMLDivElement>()
  const [yLo, yHi] = yScale?.domain ?? [0, 1]
  // The plot inside the chart: its height less the margins and the x axis.
  const plotHeight = Math.max(1, scaledHeight(280, panel) - 50)
  const plotWidth = Math.max(1, width - SCATTER_GUTTER_PX)

  // Bubbles: the largest value gets the largest radius the plot allows, and
  // every other bubble's AREA is in proportion to it.
  const largest = size ? Math.max(0, ...numbersOf(data, size)) : 0
  const maxRadius = Math.min(
    28,
    Math.max(10, Math.min(plotWidth, plotHeight) * 0.07)
  )
  const radiusOf = (row: Record<string, unknown>) =>
    size ? bubbleRadius(row[size], largest, maxRadius) : 4.5
  const sizeFmt = axisFormat({
    name: size,
    formats: panel.column_formats,
    defaultCurrency: panel.default_currency,
    scale: undefined,
  })

  // Labels of nearby points printed over each other ("Electronics" across
  // "Consumer Electronics"). Positions are estimated in pixels from the plot's
  // size, and a label that would collide with one already placed steps up or
  // down until it is clear.
  const shifted = spreadLabels(
    labelled.map((row) => ({
      text: String(row.__label ?? ""),
      px:
        typeof row[x as string] === "number" && xHi > xLo
          ? (((row[x as string] as number) - xLo) / (xHi - xLo)) * plotWidth
          : 0,
      py:
        typeof row[yKey] === "number" && yHi > yLo
          ? (1 - ((row[yKey] as number) - yLo) / (yHi - yLo)) * plotHeight
          : 0,
      left: onLeft(row),
    })),
    plotWidth
  )
  shifted.forEach((place, i) => {
    labelled[i].__dy = place.dy
    labelled[i].__left = place.left
  })

  // The best-fit line through every point, drawn only where it is on the plot.
  const fit =
    format?.trend_line && x && yKey
      ? linearFit(
          data.flatMap((d) =>
            typeof d[x] === "number" &&
            typeof d[yKey] === "number" &&
            Number.isFinite(d[x]) &&
            Number.isFinite(d[yKey])
              ? [[d[x] as number, d[yKey] as number] as const]
              : []
          )
        )
      : undefined
  const trend =
    fit && xScale && yScale
      ? clipLine(fit, xScale.domain, yScale.domain)
      : undefined

  // Written to the value's own precision, not the axis': a median of 11.5%
  // on an axis stepping by 5 read as 12%.
  const referenceText = (
    kind: keyof typeof REFERENCE_NAMES,
    value: number,
    fmt: AxisFormat
  ) =>
    [
      REFERENCE_NAMES[kind],
      formatLabelValue(value, fmt, { fractional: !Number.isInteger(value) }),
    ]
      .filter(Boolean)
      .join(" ")

  return (
    <div ref={sizeRef} className="w-full min-w-0">
      <AxisTitles left={yText.title} />
      <ChartContainer
        config={{}}
        className="aspect-auto w-full"
        style={{
          height:
            scaledHeight(280, panel) +
            (xText.title ? X_TITLE_PX : 0) +
            legendRoom(legend),
        }}
      >
        <ScatterChart margin={{ top: 12, right: 20, bottom: 8, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis
            type="number"
            dataKey={x as string}
            name={x ? seriesLabel(x, panel.column_formats) : ""}
            {...AXIS}
            domain={xScale?.domain}
            ticks={xScale?.ticks}
            tickFormatter={xText.tick}
            {...bottomTitle(xText.title, 30)}
          />
          <YAxis
            type="number"
            dataKey={yKey}
            name={seriesLabel(yKey, panel.column_formats)}
            width={VALUE_AXIS_WIDTH}
            {...AXIS}
            domain={yScale?.domain}
            ticks={yScale?.ticks}
            tickFormatter={yText.tick}
          />
          {/* Carries the size into the tooltip. The bubbles themselves are
              drawn by the shape below, whose area follows the value. */}
          {size ? (
            <ZAxis
              type="number"
              dataKey={size}
              name={seriesLabel(size, panel.column_formats, columns)}
              range={[64, 64]}
            />
          ) : null}
          <Tooltip
            content={
              <ChartTooltipBox
                formats={panel.column_formats}
                defaultCurrency={panel.default_currency}
              />
            }
            cursor={{ strokeDasharray: "3 3" }}
          />
          {legendNode(
            legend,
            groups.map((g) => g ?? "")
          )}
          {xRef !== undefined && format?.x_reference ? (
            <ReferenceLine
              x={xRef}
              stroke="var(--muted-foreground)"
              strokeDasharray="4 4"
              label={(props: unknown) => (
                <ReferenceTag
                  {...(props as { viewBox?: { x?: number; y?: number } })}
                  text={referenceText(
                    format.x_reference!.kind,
                    xRef,
                    xText.fmt
                  )}
                  vertical
                />
              )}
            />
          ) : null}
          {yRef !== undefined && format?.y_reference ? (
            <ReferenceLine
              y={yRef}
              stroke="var(--muted-foreground)"
              strokeDasharray="4 4"
              label={(props: unknown) => (
                <ReferenceTag
                  {...(props as {
                    viewBox?: { x?: number; y?: number; width?: number }
                  })}
                  text={referenceText(
                    format.y_reference!.kind,
                    yRef,
                    yText.fmt
                  )}
                  vertical={false}
                />
              )}
            />
          ) : null}
          {trend ? (
            <ReferenceLine
              segment={trend}
              stroke="var(--foreground)"
              strokeOpacity={0.55}
              strokeWidth={1.5}
              strokeDasharray="6 4"
            />
          ) : null}
          {groups.map((group, i) => {
            const points = group
              ? labelled.filter((d) => String(d[color as string]) === group)
              : labelled
            const sides = points.map((d) => d.__left === true)
            const shifts = points.map((d) => Number(d.__dy ?? 0))
            const gaps = points.map((d) => radiusOf(d) + 4)
            return (
              <Scatter
                isAnimationActive={false}
                key={group ?? "all"}
                name={group ?? seriesLabel(yKey, panel.column_formats)}
                data={points}
                fill={seriesColor(i, group, panel.palette)}
                fillOpacity={0.85}
                shape={
                  size
                    ? (props: unknown) => {
                        const point = props as {
                          cx?: number
                          cy?: number
                          fill?: string
                          payload?: Record<string, unknown>
                        }
                        if (point.cx === undefined || point.cy === undefined)
                          return <g />
                        return (
                          <circle
                            cx={point.cx}
                            cy={point.cy}
                            r={radiusOf(point.payload ?? {})}
                            fill={point.fill}
                            fillOpacity={0.7}
                            stroke="var(--background)"
                            strokeWidth={1}
                          />
                        )
                      }
                    : undefined
                }
              >
                {labels !== "none" ? (
                  <LabelList
                    dataKey="__label"
                    content={(props) => (
                      <PointLabel
                        {...(props as PointLabelProps)}
                        sides={sides}
                        shifts={shifts}
                        gaps={gaps}
                      />
                    )}
                  />
                ) : null}
              </Scatter>
            )
          })}
        </ScatterChart>
      </ChartContainer>
      {/* The size key is a legend like any other: "Hidden" hides it too. */}
      {size && largest > 0 && format?.legend !== "none" ? (
        <div className="mt-1 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <span>
            Bubble size: {seriesLabel(size, panel.column_formats, columns)}
          </span>
          {sizeLegendValues(largest).map((value) => {
            const r = bubbleRadius(value, largest, maxRadius)
            return (
              <span key={value} className="flex items-center gap-1">
                <svg width={r * 2 + 2} height={r * 2 + 2} aria-hidden="true">
                  <circle
                    cx={r + 1}
                    cy={r + 1}
                    r={r}
                    fill="var(--muted-foreground)"
                    fillOpacity={0.15}
                    stroke="var(--muted-foreground)"
                  />
                </svg>
                {formatLabelValue(value, sizeFmt)}
              </span>
            )
          })}
        </div>
      ) : null}
      {hidden > 0 ? (
        <p className="mt-1 text-center text-[11px] text-muted-foreground">
          {hidden} {hidden === 1 ? "point is" : "points are"} outside the axis
          range and not shown.
        </p>
      ) : null}
    </div>
  )
}

/**
 * Matrix heatmap: long-form rows with a row key (`color`), column key (`x`),
 * and numeric cell (`y`) — OR wide form where `y` is a LIST of bucket/metric
 * columns (one cell per column) and `color` (or `x`) names the row entity.
 *
 * Columns sort as aging buckets when labels start with a day count; rows are
 * capped to the strongest totals so a long client list stays legible.
 */
/**
 * Radar — a few named dimensions compared as a profile.
 *
 * Only legible for a handful of axes: past a dozen the labels collide and the
 * shape stops meaning anything, so the planner is told 3-8.
 */
function RadarPanel({ panel }: { panel: DashboardPanel }) {
  const { columns, rows, x } = panel
  const yFields = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []
  const data = toChartData(columns, rows)

  /* A radar's rings run from the centre outward, so a zero centre squashes
     every shape into the same near-circle when the values sit in a narrow
     band — the same complaint as a zero-anchored y axis, one geometry along. */
  const radiusDomain = fittedDomain(panel, data, yFields)

  return (
    <ChartContainer
      config={{}}
      className="aspect-auto w-full"
      style={{ height: scaledHeight(280, panel) }}
    >
      <RadarChart data={data} margin={{ top: 16, right: 24, bottom: 16, left: 24 }}>
        <PolarGrid stroke="var(--border)" />
        <PolarAngleAxis
          dataKey={x as string}
          tick={AXIS.tick}
          tickFormatter={(v) =>
            formatDateLabel(
              v,
              detectDateGranularity(rows.map((r) => r[columnIndex(columns, x ?? "")]))
            )
          }
        />
        <PolarRadiusAxis
          tick={AXIS.tick}
          domain={radiusDomain}
          tickFormatter={(v) => formatAxis(yFields[0], v, panel.column_formats, panel.default_currency)}
        />
        <Tooltip
          formatter={(value: unknown, name: unknown, item: unknown) =>
            formatCell(
              // The column, not the displayed name — see ChartTooltipBox.
              String((item as { dataKey?: unknown } | undefined)?.dataKey ?? name),
              value,
              panel.column_formats,
              null,
              panel.default_currency
            )
          }
        />
        {yFields.length > 1 ? <Legend /> : null}
        {yFields.map((field, i) => (
          <Radar
            isAnimationActive={false}
            key={field}
            dataKey={field}
            name={seriesLabel(field, panel.column_formats)}
            stroke={seriesColor(i, field, panel.palette)}
            fill={seriesColor(i, field, panel.palette)}
            fillOpacity={0.25}
          />
        ))}
      </RadarChart>
    </ChartContainer>
  )
}

/** Treemap — part-to-whole where there are too many slices for a pie. */
/** Tiles smaller than this carry no name; smaller than the next, no figure. */
const TILE_NAME_MIN = { width: 64, height: 36 }
const TILE_VALUE_MIN = { width: 40, height: 20 }
const TILE_PAD = 8

type TreemapTileProps = {
  x?: number
  y?: number
  width?: number
  height?: number
  depth?: number
  name?: string
  value?: number
  fill?: string
}

/**
 * One treemap tile: its colour, and as much of "name, then figure" as fits.
 *
 * Recharts' own tile printed the name only and dropped it altogether once the
 * tile was small, so a reader saw the size of every category and the number
 * for none. Each label here is measured against the tile before it is drawn:
 * a large tile shows both, a medium one the figure alone, a sliver nothing —
 * the legend beneath names every category either way.
 */
function TreemapTile({
  x = 0,
  y = 0,
  width = 0,
  height = 0,
  depth,
  name,
  value,
  fill,
  formatValue,
}: TreemapTileProps & { formatValue: (value: unknown) => string }) {
  // Depth 0 is the root, which spans the whole chart; only leaves are drawn.
  if (depth === 0) return null
  const room = width - TILE_PAD * 2
  const figure = formatValue(value)
  const showName =
    width >= TILE_NAME_MIN.width &&
    height >= TILE_NAME_MIN.height &&
    Boolean(name)
  const showValue =
    width >= TILE_VALUE_MIN.width &&
    height >= TILE_VALUE_MIN.height &&
    tickLabelPx(figure) <= room
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={fill}
        stroke="var(--background)"
        strokeWidth={2}
      />
      {showName ? (
        <text
          x={x + TILE_PAD}
          y={y + TILE_PAD + 11}
          fill="#fff"
          // The tile's border stroke is inherited by text unless it is
          // cleared, and outlines every letter.
          stroke="none"
          fontSize={12}
          fontWeight={600}
        >
          {truncateToPx(String(name), room)}
        </text>
      ) : null}
      {showValue ? (
        <text
          x={x + TILE_PAD}
          // Under the name when there is one; otherwise centred in the tile.
          y={showName ? y + TILE_PAD + 27 : y + height / 2 + 4}
          fill="#fff"
          stroke="none"
          fillOpacity={0.9}
          fontSize={11}
          className="tabular-nums"
        >
          {figure}
        </text>
      ) : null}
    </g>
  )
}

function TreemapPanel({ panel }: { panel: DashboardPanel }) {
  const { columns, rows, x } = panel
  const yName = Array.isArray(panel.y) ? panel.y[0] : panel.y
  const xi = columnIndex(columns, x ?? "")
  const yi = columnIndex(columns, yName ?? "")

  // Recharts sizes tiles by `size`; a negative or zero area cannot be drawn,
  // so those rows are dropped rather than rendering an invisible tile.
  const data = rows
    .map((row, i) => ({
      name: String(row[xi >= 0 ? xi : 0] ?? ""),
      size: Number(row[yi >= 0 ? yi : 1]) || 0,
      fill: seriesColor(i, String(row[xi >= 0 ? xi : 0] ?? ""), panel.palette),
    }))
    .filter((d) => d.size > 0)

  // Compact on the tiles and in the legend ("AED 41.7M"); the tooltip keeps
  // the full figure.
  const formatValue = (value: unknown) =>
    formatAxis(yName ?? "", value, panel.column_formats, panel.default_currency)
  const total = data.reduce((sum, d) => sum + d.size, 0)
  const byValue = [...data].sort((a, b) => b.size - a.size)

  return (
    <div className="w-full min-w-0">
      <ChartContainer
        config={{}}
        className="aspect-auto w-full"
        style={{ height: scaledHeight(280, panel) }}
      >
        <Treemap
          isAnimationActive={false}
          data={data}
          dataKey="size"
          nameKey="name"
          stroke="var(--background)"
          content={<TreemapTile formatValue={formatValue} />}
        >
          <Tooltip
            formatter={(value: unknown) =>
              formatCell(yName ?? "", value, panel.column_formats, null, panel.default_currency)
            }
          />
        </Treemap>
      </ChartContainer>
      {/* Every category, largest first, with its figure and share. A tile too
          small to carry its own name is still named here, so no category is
          visible only as an unlabelled sliver. */}
      <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
        {byValue.map((d) => (
          <li key={d.name} className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="size-2.5 shrink-0 rounded-[3px]"
              style={{ backgroundColor: d.fill }}
            />
            <span className="text-foreground">{d.name}</span>
            <span className="tabular-nums">{formatValue(d.size)}</span>
            {total > 0 ? (
              <span className="tabular-nums">
                · {((d.size / total) * 100).toFixed(1)}%
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Funnel — stages of a process, largest first. */
function FunnelPanel({ panel }: { panel: DashboardPanel }) {
  const [sizeRef, width] = useMeasuredWidth<HTMLDivElement>()
  const { columns, rows, x } = panel
  const yName = Array.isArray(panel.y) ? panel.y[0] : panel.y
  const xi = columnIndex(columns, x ?? "")
  const yi = columnIndex(columns, yName ?? "")

  /* The stage name AND its figure, in ONE label.
     Two label lists — a name outside and a value inside the trapezoid — would
     need the text colour to contrast with whichever palette colour that stage
     happens to be, and a pale stage renders white-on-cream. One label outside
     always sits on the panel background, so it always reads. */
  /* Largest stage at the top, so the funnel narrows and its percentages fall
     from 100% — see sortFunnelStages. */
  const stages = sortFunnelStages(
    rows.map((row) => ({
      name: String(row[xi >= 0 ? xi : 0] ?? ""),
      value: Number(row[yi >= 0 ? yi : 1]) || 0,
    }))
  )
  /* A second, quieter line under each label: 100% at the top, then each
     stage's share of the top (or of the stage above, or nothing), as the
     panel's funnel_percent setting says. */
  const notes = funnelStageNotes(
    stages.map((s) => s.value),
    stages.map((s) => s.name),
    panel.format?.funnel_percent ?? "first"
  )
  const data = stages.map(({ name, value }, i) => ({
    name,
    value,
    label: `${name}  ${formatCell(yName ?? "", value, panel.column_formats, null, panel.default_currency)}`,
    note: notes[i] ?? "",
    fill: seriesColor(i, name, panel.palette),
  }))

  /* The gutter is measured from the longest label rather than fixed at 80px,
     which silently clipped anything longer than about twelve characters —
     "Authentication Attempts" lost its last word with nothing to show it had
     been cut. Capped at 45% of the panel so the funnel itself stays the
     larger half. */
  // The note is set a size smaller, so its characters count for a little less.
  const longestLabel = data.reduce(
    (max, d) => Math.max(max, d.label.length, Math.ceil(d.note.length * 0.92)),
    0
  )
  const labelGutter = Math.max(96, Math.min(280, longestLabel * CHAR_PX + 18))

  /* Every label starts at the SAME x, not at its own stage's right edge.
     `position="right"` anchors each one to the trapezoid it belongs to, and a
     funnel's trapezoids are all different widths — so the labels stepped in
     and out down the panel, and a narrow stage's label ran underneath the
     wider stage below it. A fixed column reads as a list, which is what it is. */
  const labelX = width > 0 ? width - labelGutter + 10 : 0

  return (
    <div ref={sizeRef} className="w-full min-w-0">
      <ChartContainer
        config={{}}
        className="aspect-auto w-full"
        style={{ height: scaledHeight(280, panel) }}
      >
        <FunnelChart
          margin={{ top: 12, right: labelGutter, bottom: 12, left: 24 }}
        >
          <Tooltip
            formatter={(value: unknown) =>
              formatCell(yName ?? "", value, panel.column_formats, null, panel.default_currency)
            }
          />
          <Funnel dataKey="value" data={data} isAnimationActive={false}>
            <LabelList
              dataKey="label"
              position="right"
              content={(props) => {
                /* Recharts types x/y/height as string | number — they are
                   numbers for a funnel, but the signature has to accept both
                   or the renderer will not take this component. */
                const n = (v: unknown) => (typeof v === "number" ? v : Number(v) || 0)
                // Before the first measurement lands there is no column to
                // align to, so fall back to the stage's own edge.
                const cx = labelX || n(props.x)
                const cy = n(props.y) + n(props.height) / 2
                const note = data[n(props.index)]?.note ?? ""
                /* Two lines when the stage is tall enough to hold them, centred
                   together on the stage; one line, the note after the figure,
                   when it is not — the column stays aligned either way. */
                const twoLines = note !== "" && n(props.height) >= 30
                return (
                  <text
                    x={cx}
                    y={twoLines ? cy - 7 : cy}
                    fill="var(--foreground)"
                    fontSize={11}
                    dominantBaseline="middle"
                    textAnchor="start"
                  >
                    {String(props.value ?? "")}
                    {note !== "" && !twoLines && (
                      <tspan fill="var(--muted-foreground)" fontSize={10}>{`  ·  ${note}`}</tspan>
                    )}
                    {twoLines && (
                      <tspan x={cx} dy={14} fill="var(--muted-foreground)" fontSize={10}>
                        {note}
                      </tspan>
                    )}
                  </text>
                )
              }}
            />
          </Funnel>
        </FunnelChart>
      </ChartContainer>
    </div>
  )
}

/**
 * Combo — bars with a line over them.
 *
 * The one case a stacked bar cannot cover: a count and a rate share an x axis
 * but not a scale, so the line gets its own axis on the right.
 */
function ComboPanel({ panel }: { panel: DashboardPanel }) {
  const { columns, rows, x } = panel
  const yFields = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []
  const data = toChartData(columns, rows)
  const categories = rows.map((r) => r[columnIndex(columns, x ?? "")])
  const dateGrain = detectDateGranularity(categories)
  const acrossYears = spansYears(categories)
  const axisLabel = (v: unknown) =>
    formatDateLabel(v, dateGrain, true, acrossYears)
  const axisLabels = categories.map(axisLabel)
  // Laid out like every other category axis: a run of weeks thins evenly,
  // named categories are all shown. Left to itself Recharts dropped labels
  // that collided, and a chart of stores lost every other name.
  const [sizeRef, width] = useMeasuredWidth<HTMLDivElement>()
  const plan = axisPlan(axisLabels, false, yFields.length, width, {
    allowHorizontal: false,
    sequence: dateGrain !== null,
  })
  // Last series becomes the line; everything before it stays a bar.
  const barFields = yFields.slice(0, -1)
  const lineField = yFields[yFields.length - 1]

  /* Each axis fits its own series. The right-hand axis is the one this panel
     exists for — a rate whose whole story sits between 94% and 98% is a flat
     line against a zero baseline. */
  /* The line has to read AGAINST the bars, so it does not simply take the
     next series colour — inside a themed palette that is a near neighbour of
     the bar colour, and a thin line over thick bars of nearly the same green
     is invisible. */
  const barColours = barFields.map((field, i) =>
    seriesColor(i, field, panel.palette)
  )
  const lineColour = accentColor(panel.palette, barColours)

  const barScale = standardScale(panel, data, barFields)
  const lineScale = lineField
    ? standardScale(panel, data, [lineField])
    : undefined
  // Two axes, two titles: the left one names the bars, the right one the line.
  const barText = useAxisText(panel, barFields, barScale)
  const lineText = useAxisText(panel, lineField ? [lineField] : [], lineScale, {
    setting: "none",
  })
  const xTitle = categoryTitle(panel, x, categories)
  const legend = legendFor(yFields.length, panel.format?.legend)
  const labelsOf = (key: string) =>
    seriesLabels(panel.chart_type, data.length, panel.format, key)
  const lineMarkers = lineField
    ? seriesMarkers(panel.format, lineField, data.length)
    : "none"
  const labelTop = yFields.some((key) => labelsOf(key) !== "none")
    ? LABEL_ROOM
    : 0

  return (
    <div ref={sizeRef} className="w-full min-w-0">
      <AxisTitles left={barText.title} right={lineText.title} />
      <ChartContainer
        config={{}}
        className="aspect-auto w-full"
        style={{
          height:
            scaledHeight(280, panel) +
            (plan.height - CHART_HEIGHT) +
            (xTitle ? X_TITLE_PX : 0) +
            labelTop,
        }}
      >
        <ComposedChart
          data={data}
          margin={{
            top: 12 + labelTop,
            right: 16,
            bottom: 8,
            left: axisLeftMargin(plan, axisLabels),
          }}
        >
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis
            dataKey={x as string}
            {...AXIS}
            tickFormatter={(v) => truncateToPx(axisLabel(v), plan.maxLabelPx)}
            {...categoryTickProps(plan, axisLabel)}
            {...bottomTitle(xTitle, plan.tickHeight)}
          />
          <YAxis
            yAxisId="left"
            {...AXIS}
            width={VALUE_AXIS_WIDTH}
            domain={barScale?.domain}
            ticks={barScale?.ticks}
            tickFormatter={barText.tick}
          />
          <YAxis
            yAxisId="right"
            orientation="right"
            {...AXIS}
            width={VALUE_AXIS_WIDTH}
            domain={lineScale?.domain}
            ticks={lineScale?.ticks}
            tickFormatter={lineText.tick}
          />
          <Tooltip
            content={
              <ChartTooltipBox
                formats={panel.column_formats}
                defaultCurrency={panel.default_currency}
                dateGrain={dateGrain}
              />
            }
          />
          {legendNode(legend, yFields)}
          {barFields.map((field, i) => (
            <Bar
              isAnimationActive={false}
              key={field}
              yAxisId="left"
              dataKey={field}
              name={seriesLabel(field, panel.column_formats)}
              fill={barColours[i]}
            />
          ))}
          {lineField ? (
            <Line
              isAnimationActive={false}
              yAxisId="right"
              type="monotone"
              dataKey={lineField}
              name={seriesLabel(lineField, panel.column_formats)}
              stroke={lineColour}
              strokeWidth={TREND_STROKE}
              dot={lineMarkers === "all" ? markerDot(lineColour) : false}
              activeDot={{ r: 5 }}
            />
          ) : null}
          <ValueLabels
            data={data}
            categoryKey={x as string}
            unit={panel.format?.unit}
            barSlots={barFields.length}
            valueDomain={barScale?.domain}
            series={[
              ...barFields.map((field, i) => ({
                key: field,
                color: barColours[i],
                fmt: barText.fmt,
                shape: "bar" as const,
                labels: labelsOf(field),
                position: seriesLabelPosition(panel.format, field),
                yAxisId: "left",
                barSlot: i,
                bare: titleCarriesCurrency(barText.fmt, barText.title),
              })),
              ...(lineField
                ? [
                    {
                      key: lineField,
                      color: lineColour,
                      fmt: lineText.fmt,
                      shape: "point" as const,
                      labels: labelsOf(lineField),
                      markers: lineMarkers,
                      yAxisId: "right",
                      bare: titleCarriesCurrency(lineText.fmt, lineText.title),
                    },
                  ]
                : []),
            ]}
          />
        </ComposedChart>
      </ChartContainer>
    </div>
  )
}

/**
 * A measure as a filled area, with a target or threshold over it.
 *
 * Distinct from ComboPanel in the one way that matters: BOTH series share a
 * single axis. Combo exists for a count and a rate, which have no common scale
 * and need two. This exists for two series in the SAME unit — actual against
 * target, this year against last — where two axes would be a lie: the reader
 * compares the line's height to the area's, and that comparison is only
 * truthful if they are measured against the same ticks.
 *
 * The last series is the reference line, matching the convention combo already
 * set, and it is dashed because a target is not an observation.
 */
function AreaLinePanel({ panel }: { panel: DashboardPanel }) {
  const { columns, rows, x } = panel
  const yFields = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []
  const data = toChartData(columns, rows)
  const categories = rows.map((r) => r[columnIndex(columns, x ?? "")])
  const dateGrain = detectDateGranularity(categories)
  const acrossYears = spansYears(categories)
  const axisLabel = (v: unknown) =>
    formatDateLabel(v, dateGrain, true, acrossYears)
  const axisLabels = categories.map(axisLabel)
  // Laid out like every other category axis: a run of weeks thins evenly,
  // named categories are all shown. Left to itself Recharts dropped labels
  // that collided, and a chart of stores lost every other name.
  const [sizeRef, width] = useMeasuredWidth<HTMLDivElement>()
  const plan = axisPlan(axisLabels, false, yFields.length, width, {
    allowHorizontal: false,
    sequence: dateGrain !== null,
  })

  // One series is just an area chart; there is no reference line to split off.
  const areaFields = yFields.length > 1 ? yFields.slice(0, -1) : yFields
  const lineField = yFields.length > 1 ? yFields[yFields.length - 1] : null

  const areaColours = areaFields.map((field, i) =>
    seriesColor(i, field, panel.palette)
  )
  // Same reasoning as combo: inside a themed palette the next series colour is
  // a near neighbour of the fill, and a thin line over it disappears.
  const lineColour = accentColor(panel.palette, areaColours)

  // One scale across EVERY series together. Scaling them separately is what a
  // second axis does, and it would put a target below its own measure. Zero is
  // kept in range for the same reason bars keep it: the fill is measured from
  // a baseline, and a baseline that floats with the data measures nothing.
  const scale = standardScale(panel, data, yFields)
  const valueText = useAxisText(panel, areaFields, scale)
  const xTitle = categoryTitle(panel, x, categories)
  const legend = legendFor(yFields.length, panel.format?.legend)
  const labelsOf = (key: string, reference = false) =>
    seriesLabels(panel.chart_type, data.length, panel.format, key, {
      reference,
    })
  const markersOf = (key: string, reference = false) =>
    seriesMarkers(panel.format, key, data.length, { reference })
  const labelTop =
    areaFields.some((key) => labelsOf(key) !== "none") ||
    (lineField !== null && labelsOf(lineField, true) !== "none")
      ? LABEL_ROOM
      : 0

  return (
    <div ref={sizeRef} className="w-full min-w-0">
      <AxisTitles left={valueText.title} />
      <ChartContainer
        config={{}}
        className="aspect-auto w-full"
        style={{
          height:
            scaledHeight(280, panel) +
            (plan.height - CHART_HEIGHT) +
            (xTitle ? X_TITLE_PX : 0) +
            labelTop,
        }}
      >
        <ComposedChart
          data={data}
          margin={{
            top: 12 + labelTop,
            right: 16,
            bottom: 8,
            left: axisLeftMargin(plan, axisLabels, { centredOnEdge: true }),
          }}
        >
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis
            dataKey={x as string}
            {...AXIS}
            tickFormatter={(v) => truncateToPx(axisLabel(v), plan.maxLabelPx)}
            {...categoryTickProps(plan, axisLabel)}
            {...bottomTitle(xTitle, plan.tickHeight)}
          />
          <YAxis
            {...AXIS}
            width={VALUE_AXIS_WIDTH}
            domain={scale?.domain}
            ticks={scale?.ticks}
            tickFormatter={valueText.tick}
          />
          <Tooltip
            content={
              <ChartTooltipBox
                formats={panel.column_formats}
                defaultCurrency={panel.default_currency}
                dateGrain={dateGrain}
              />
            }
          />
          {legendNode(legend, yFields)}
          {areaFields.map((field, i) => (
            <Area
              isAnimationActive={false}
              key={field}
              type="monotone"
              dataKey={field}
              name={seriesLabel(field, panel.column_formats)}
              stroke={areaColours[i]}
              fill={areaColours[i]}
              fillOpacity={areaFields.length > 1 ? 0.18 : 0.28}
              strokeWidth={LINE_STROKE}
              dot={
                markersOf(field) === "all" ? markerDot(areaColours[i]) : false
              }
              activeDot={{ r: 5 }}
              connectNulls
            />
          ))}
          {lineField ? (
            <Line
              isAnimationActive={false}
              type="monotone"
              dataKey={lineField}
              name={seriesLabel(lineField, panel.column_formats)}
              stroke={lineColour}
              strokeWidth={TREND_STROKE}
              strokeDasharray="6 4"
              dot={
                markersOf(lineField, true) === "all"
                  ? markerDot(lineColour)
                  : false
              }
              connectNulls
            />
          ) : null}
          <ValueLabels
            data={data}
            categoryKey={x as string}
            unit={panel.format?.unit}
            series={[
              ...areaFields.map((field, i) => ({
                key: field,
                color: areaColours[i],
                fmt: valueText.fmt,
                shape: "point" as const,
                labels: labelsOf(field),
                markers: markersOf(field),
                bare: titleCarriesCurrency(valueText.fmt, valueText.title),
              })),
              ...(lineField
                ? [
                    {
                      key: lineField,
                      color: lineColour,
                      fmt: valueText.fmt,
                      shape: "point" as const,
                      labels: labelsOf(lineField, true),
                      markers: markersOf(lineField, true),
                      bare: titleCarriesCurrency(
                        valueText.fmt,
                        valueText.title
                      ),
                    },
                  ]
                : []),
            ]}
          />
        </ComposedChart>
      </ChartContainer>
    </div>
  )
}

/** Columns in one block of a printed heatmap; wider ones continue below. */
const PRINT_HEATMAP_COLUMNS = 9

function HeatmapPanel({ panel, print = false }: { panel: DashboardPanel; print?: boolean }) {
  const { columns, rows, x, color } = panel
  const yFields = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []

  // Wide matrix: y is several numeric columns (aging buckets as aliases).
  // Long-form: one metric column + x = bucket + color = row group.
  const wide = yFields.length > 1
  const valueKey = wide ? null : yFields[0]
  const colKey = wide ? null : x
  const rowKey = color ?? (wide ? x : null)

  if (!wide && (!colKey || !valueKey)) {
    return <TablePanel panel={panel} />
  }
  if (wide && !rowKey) {
    return <TablePanel panel={panel} />
  }

  const colIdx = colKey ? columnIndex(columns, colKey) : -1
  const valIdx = valueKey ? columnIndex(columns, valueKey) : -1
  const rowIdx = rowKey ? columnIndex(columns, rowKey) : -1

  if (!wide && (colIdx < 0 || valIdx < 0)) {
    return <TablePanel panel={panel} />
  }
  if (wide && rowIdx < 0) {
    return <TablePanel panel={panel} />
  }

  const wideColIdx = wide
    ? yFields.map((name) => ({ name, idx: columnIndex(columns, name) }))
        .filter((c) => c.idx >= 0)
    : []

  if (wide && wideColIdx.length === 0) {
    return <TablePanel panel={panel} />
  }

  const colLabelsRaw: string[] = []
  const rowLabelsRaw: string[] = []
  const seenCol = new Set<string>()
  const seenRow = new Set<string>()
  const cells = new Map<string, number>()

  if (wide) {
    for (const { name } of wideColIdx) {
      colLabelsRaw.push(name)
      seenCol.add(name)
    }
    for (const row of rows) {
      const r = String(row[rowIdx] ?? "")
      if (!seenRow.has(r)) {
        seenRow.add(r)
        rowLabelsRaw.push(r)
      }
      for (const { name, idx } of wideColIdx) {
        const raw = row[idx]
        const v = typeof raw === "number" ? raw : Number(raw)
        if (Number.isFinite(v)) {
          cells.set(`${r}\0${name}`, v)
        }
      }
    }
  } else {
    for (const row of rows) {
      const c = String(row[colIdx] ?? "").trim()
      if (!c) continue
      const r = rowIdx >= 0 ? String(row[rowIdx] ?? "") : ""
      const raw = row[valIdx]
      const v = typeof raw === "number" ? raw : Number(raw)
      if (!seenCol.has(c)) {
        seenCol.add(c)
        colLabelsRaw.push(c)
      }
      if (rowIdx >= 0 && !seenRow.has(r)) {
        seenRow.add(r)
        rowLabelsRaw.push(r)
      }
      if (Number.isFinite(v)) {
        cells.set(`${r}\0${c}`, v)
      }
    }
  }

  const colLabels = sortHeatLabels(colLabelsRaw)
  let rowLabels =
    rowIdx >= 0 ? rankHeatRows(rowLabelsRaw, colLabels, cells) : [""]
  if (rowLabels.length === 0) rowLabels = [""]

  const truncatedRows =
    rowIdx >= 0 && rowLabelsRaw.length > rowLabels.length
      ? rowLabelsRaw.length - rowLabels.length
      : 0

  let min = Infinity
  let max = -Infinity
  for (const r of rowLabels) {
    for (const c of colLabels) {
      const v = cells.get(`${r}\0${c}`)
      if (typeof v === "number" && Number.isFinite(v)) {
        if (v < min) min = v
        if (v > max) max = v
      }
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    min = 0
    max = 1
  }
  const span = max - min || 1

  const sampleMetric = valueKey ?? colLabels[0] ?? "VALUE"
  const formatVal = (v: number | undefined, compact = false) => {
    if (v === undefined) return "—"
    // The panel's declared formats, or a heatmap of dirhams prints dollars in
    // every cell while the table underneath it does not.
    return compact
      ? formatAxis(sampleMetric, v, panel.column_formats, panel.default_currency)
      : formatCell(sampleMetric, v, panel.column_formats, null, panel.default_currency)
  }

  const cornerLabel = rowKey
    ? String(rowKey)
        .replace(/_/g, " ")
        .replace(/\b\w/g, (ch) => ch.toUpperCase())
    : ""

  // A month column stored as "2025-02-01" reads 02-2025, as it does on every
  // other chart's axis.
  const columnGrain = detectDateGranularity(colLabels)
  const columnAcrossYears = spansYears(colLabels)
  const columnName = (c: string) =>
    columnGrain ? formatDateLabel(c, columnGrain, true, columnAcrossYears) : c.replace(/_/g, " ")

  /* On screen the grid scrolls; on paper nothing scrolls, so a printed
     heatmap is drawn whole, its columns split into blocks that fit the page. */
  const blocks: string[][] = []
  if (print) {
    for (let i = 0; i < colLabels.length; i += PRINT_HEATMAP_COLUMNS) {
      blocks.push(colLabels.slice(i, i + PRINT_HEATMAP_COLUMNS))
    }
  } else {
    blocks.push(colLabels)
  }

  return (
    <div className="flex w-full min-w-0 flex-col gap-2">
      {blocks.map((block, blockIndex) => (
      <div
        key={blockIndex}
        className={cn(
          "w-full min-w-0 rounded-md border border-border/50",
          !print && "scrollbar-slim overflow-auto overscroll-contain"
        )}
        style={print ? undefined : { maxHeight: scaledHeight(340, panel) }}
      >
        <table className="w-auto min-w-full border-separate border-spacing-1 p-1 text-[11.5px]">
          <thead>
            <tr>
              {rowIdx >= 0 ? (
                <th
                  className="sticky top-0 left-0 z-20 bg-card px-2.5 py-2 text-left text-[10px] font-semibold tracking-wide text-muted-foreground uppercase"
                >
                  {cornerLabel}
                </th>
              ) : null}
              {block.map((c) => (
                <th
                  key={c}
                  className="sticky top-0 z-10 min-w-[4.75rem] bg-card px-2 py-2 text-center text-[11px] font-semibold whitespace-nowrap text-foreground"
                >
                  {columnName(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rowLabels.map((r) => (
              <tr key={r || "all"}>
                {rowIdx >= 0 ? (
                  <th className="sticky left-0 z-10 max-w-[10rem] truncate bg-card px-2.5 py-2 text-left font-medium text-foreground">
                    {r}
                  </th>
                ) : null}
                {block.map((c) => {
                  const v = cells.get(`${r}\0${c}`)
                  const missing = v === undefined
                  const t = missing ? 0 : (v - min) / span
                  const bg = missing ? undefined : heatColor(t)
                  const dark = !missing && t > 0.55
                  return (
                    <td
                      key={c}
                      className={cn(
                        "min-w-[4.75rem] rounded-md px-2 py-2.5 text-center tabular-nums",
                        missing
                          ? "border border-dashed border-border/50 text-muted-foreground/50"
                          : dark
                            ? "text-white"
                            : "text-foreground"
                      )}
                      style={bg ? { background: bg } : undefined}
                      title={
                        missing
                          ? undefined
                          : `${rowIdx >= 0 ? `${r} · ` : ""}${columnName(c)}: ${formatVal(v)}`
                      }
                    >
                      {formatVal(v, true)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      ))}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1 text-[10px] text-muted-foreground">
        <span className="tabular-nums">{formatVal(min, true)}</span>
        <div
          className="h-2 min-w-[4rem] flex-1 rounded-full"
          style={{
            background: `linear-gradient(to right, ${heatColor(0)}, ${heatColor(1)})`,
          }}
        />
        <span className="tabular-nums">{formatVal(max, true)}</span>
        {truncatedRows > 0 ? (
          <span className="w-full text-[10px] text-muted-foreground/80">
            Showing top {rowLabels.length} of{" "}
            {rowLabels.length + truncatedRows} by total intensity
          </span>
        ) : null}
      </div>
    </div>
  )
}

export function DashboardPanelCard({
  panel,
  onCategoryClick,
  selectedValue,
}: {
  panel: DashboardPanel
  onCategoryClick?: (value: string) => void
  selectedValue?: string | null
}) {
  if (panel.error) {
    return (
      <PanelShell panel={panel}>
        <div className="flex flex-1 flex-col items-center justify-center gap-1 py-8 text-center">
          <p className="text-sm font-medium text-destructive">
            Couldn’t load this panel
          </p>
          <p className="max-w-sm text-xs text-muted-foreground">{panel.error}</p>
        </div>
      </PanelShell>
    )
  }

  if (panel.rows.length === 0) {
    return (
      <PanelShell panel={panel}>
        <div className="flex flex-1 flex-col items-center justify-center gap-1 py-8 text-center">
          <p className="text-sm font-medium text-foreground/80">No data</p>
          <p className="text-xs text-muted-foreground">
            This query returned no rows for the current filters.
          </p>
        </div>
      </PanelShell>
    )
  }

  const type = panel.chart_type ?? ""
  const isKpi = type === "kpi" || panel.display === "kpi"

  if (isKpi) {
    return (
      <PanelShell panel={panel} bare>
        <KpiPanel panel={panel} />
      </PanelShell>
    )
  }

  /* `display` says how to PRESENT the panel; `chart_type` says which chart to
     draw. When they disagree — display "table" on a panel that also names a
     funnel, with x, y and a chart-shaped query behind it — the chart wins.
     Choosing a type is the deliberate act, and the rows stay one click away in
     the disclosure under every chart, so nothing is lost either way. This read
     the other way round for a long time, and a good chart was silently replaced
     by a grid of numbers with nothing on screen to say why.

     New plans are reconciled server-side in dashboards._normalise_display; this
     covers dashboards saved before that existed. A panel with no chart type has
     nothing to draw and is a table whatever display claims. */
  if (!type || type === "table") {
    return (
      <PanelShell panel={panel}>
        <TablePanel panel={panel} />
      </PanelShell>
    )
  }

  return (
    <PanelShell panel={panel}>
      <PanelChart
        panel={panel}
        onCategoryClick={onCategoryClick}
        selectedValue={selectedValue}
      />
      {/* The chart is the answer; the table is the audit trail. Shown open, it
          doubled every panel's height and — at half width, with a horizontal
          scrollbar — was unreadable anyway. Collapsed, the values stay one
          click away for anyone checking a number, which is the same bargain
          the SQL disclosure in chat makes. */}
      {/* Shown for EVERY chart, not only display="both". It is collapsed, so
          it costs one line, and a chart whose numbers cannot be checked is
          the thing people distrust — most panels were arriving as
          display="chart" and losing the disclosure entirely. */}
      {panel.rows.length > 0 ? <DataDisclosure panel={panel} /> : null}
    </PanelShell>
  )
}

/**
 * A panel's chart alone: no card, title or data disclosure.
 *
 * The card draws it on the dashboard, and the PDF export draws it off screen
 * to take its picture, so the two can never show different charts.
 */
export function PanelChart({
  panel,
  onCategoryClick,
  selectedValue,
  print = false,
}: {
  panel: DashboardPanel
  onCategoryClick?: (value: string) => void
  selectedValue?: string | null
  /** Drawn for paper: nothing scrolls, so a chart that scrolls on screen is drawn whole. */
  print?: boolean
}) {
  const type = panel.chart_type ?? ""
  const chart =
    /* grouped_bar was absent here and fell through to the table fallback, so a
       panel planned as grouped bars rendered as a grid of numbers. BarPanel
       already draws it correctly: `stacked` is false for it, so a colour column
       splits the value into one series per distinct value, which IS a grouped
       bar. The PDF exporter has always rendered it. */
    type === "bar" || type === "stacked_bar" || type === "grouped_bar" ? (
      <BarPanel
        panel={panel}
        onCategoryClick={onCategoryClick}
        selectedValue={selectedValue}
      />
    ) : type === "line" || type === "area" ? (
      <LinePanel panel={panel} />
    ) : type === "pie" || type === "donut" || type === "doughnut" ? (
      <PiePanel
        panel={panel}
        onCategoryClick={onCategoryClick}
        selectedValue={selectedValue}
      />
    ) : type === "scatter" || type === "bubble" ? (
      <ScatterPanel panel={panel} />
    ) : type === "heatmap" ? (
      <HeatmapPanel panel={panel} print={print} />
    ) : type === "geo_us_states" || type === "geo_countries" ? (
      <GeoPanel panel={panel} />
    ) : type === "radar" ? (
      <RadarPanel panel={panel} />
    ) : type === "treemap" ? (
      <TreemapPanel panel={panel} />
    ) : type === "funnel" ? (
      <FunnelPanel panel={panel} />
    ) : type === "combo" ? (
      <ComboPanel panel={panel} />
    ) : type === "area_line" ? (
      <AreaLinePanel panel={panel} />
    ) : (
      // Chart types without a native renderer degrade to a table rather than
      // rendering nothing. funnel / histogram are intentionally not planned —
      // see the LLM chart cookbook.
      <TablePanel panel={panel} />
    )
  return chart
}

/** The collapsed rows under a chart. */
function DataDisclosure({ panel }: { panel: DashboardPanel }) {
  return (
    <details className="group mt-3 min-w-0">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
        <ChevronRightIcon
          className="size-3 shrink-0 transition-transform group-open:rotate-90"
          aria-hidden
        />
        Data
        <span className="text-muted-foreground/70">
          · {panel.rows.length} {panel.rows.length === 1 ? "row" : "rows"}
        </span>
        {panel.truncated ? (
          /* Stated, not implied: a capped series and a genuinely short
                 one look identical on the chart above. Worded without a count
                 because a cross-filtered panel keeps the flag while its row
                 total no longer matches what the warehouse returned. */
          <span className="text-amber-700 dark:text-amber-500">
            · the query returns more rows than a dashboard loads
          </span>
        ) : null}
      </summary>
      <div className="mt-2">
        <TablePanel panel={panel} />
      </div>
    </details>
  )
}
