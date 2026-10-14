import type { DashboardPanel } from "@/components/dashboard/dashboard-panel"

/**
 * Deciding where panels go, instead of letting CSS decide by accident.
 *
 * A panel's width comes from its stored `span` when it has one — the planner
 * sets it, and an editor can drag it. Older dashboards were saved before spans
 * existed, so when a section has none the widths are DERIVED from each panel's
 * shape and packed to avoid holes. Both paths end in the same 4-column grid.
 *
 * Charts default to half (2) or full (4). Quarter-width (1) is never derived
 * and is upgraded when reading stored layouts — charts that narrow are
 * illegible. KPIs leave the grid for a dedicated band above it.
 *
 * The derived path exists because a 2-column grid places items in source order,
 * and a full-width item cannot sit beside a half-width one — so a full-width
 * panel arriving mid-row drops to the next row and leaves a hole above it:
 *
 *   bar (half) | HOLE
 *   bar (full)
 *   pie (half) | HOLE
 *   bar (full)
 *
 * Two holes in four panels, and nothing in the data was wrong — only the order
 * it was emitted in.
 */

/** The grid is 4 columns wide; a span of 1-4 is quarter through full. */
export const GRID_COLUMNS = 4
export const MAX_ROW_SPAN = 4

/**
 * Narrowest width Arrange may assign to a body chart.
 *
 * Quarter-width (1) makes axes and legends unreadable. Half (2) is the floor.
 */
export const MIN_CHART_SPAN: PanelSpan = 2

/**
 * Height steps, where 2 — NOT 1 — is the normal height.
 *
 * A chart is given an explicit pixel height because Recharts needs a definite
 * one, so the container cannot shrink it: without a step below the default
 * there is no way to turn a tall rectangle into something squarer. Step 1 is
 * that step.
 */
export const DEFAULT_ROW_SPAN: PanelRowSpan = 2

export type PanelSpan = 1 | 2 | 3 | 4
export type PanelRowSpan = 1 | 2 | 3 | 4

export type LaidOutPanel = {
  panel: DashboardPanel
  span: PanelSpan
  rowSpan: PanelRowSpan
  /** Index into the ORIGINAL panel array, so an edit can be saved back. */
  index: number
}

export type DashboardSection = {
  name: string | null
  /** Headline tiles for this section — rendered above the chart grid. */
  kpis: DashboardPanel[]
  /** Body panels only (charts / tables), laid out on the 4-column grid. */
  panels: LaidOutPanel[]
}

/** The name shown on the tab when the planner grouped panels into one section. */
export const DEFAULT_SECTION = "Overview"

/** A headline-number panel. These leave the grid entirely for the KPI band. */
export function isKpiPanel(panel: DashboardPanel): boolean {
  return panel.chart_type === "kpi" || panel.display === "kpi"
}

export function clampSpan(value: unknown): PanelSpan | null {
  const n = typeof value === "number" ? value : Number(value)
  if (!Number.isFinite(n)) return null
  return Math.max(1, Math.min(GRID_COLUMNS, Math.round(n))) as PanelSpan
}

export function clampRowSpan(value: unknown): PanelRowSpan | null {
  const n = typeof value === "number" ? value : Number(value)
  if (!Number.isFinite(n)) return null
  return Math.max(1, Math.min(MAX_ROW_SPAN, Math.round(n))) as PanelRowSpan
}

/**
 * Width for a body panel: never quarter. Stored `1` upgrades to half so
 * dashboards saved under the cramped Arrange defaults still read like the
 * intended 2-up chart layout.
 */
export function chartSpan(panel: DashboardPanel, raw: unknown): PanelSpan {
  const stored = clampSpan(raw)
  if (stored !== null) {
    return stored < MIN_CHART_SPAN ? MIN_CHART_SPAN : stored
  }
  return panelWidth(panel) === "full" ? GRID_COLUMNS : MIN_CHART_SPAN
}

/**
 * How much width a panel earns when nothing was stored for it.
 *
 * An earlier rule was `rows.length > 12`, which let a DATA property stand in
 * for a DESIGN decision: a 20-row bar chart is not more important than a 4-row
 * one, and promoting it to full width both wasted space and punched a hole in
 * the row above it.
 *
 * Width is about how much horizontal room the chart genuinely needs: tables
 * want all of it, a pie never does, and a categorical chart earns it only when
 * it has enough categories that half a row would crowd the labels.
 */
export function panelWidth(panel: DashboardPanel): "half" | "full" {
  // KPIs leave the grid for the top band; if one still reaches here, full width.
  if (isKpiPanel(panel)) return "full"
  if (panel.display === "table" || !panel.chart_type) return "full"

  const type = panel.chart_type
  // A pie is a circle. Extra width becomes margin, never legibility.
  if (type === "pie" || type === "donut" || type === "doughnut") return "half"

  const categories = panel.rows.length
  const series = Array.isArray(panel.y) ? panel.y.length : 1

  // Grouped bars multiply the categories competing for the same axis.
  if (series > 1 && categories > 8) return "full"
  return categories > 14 ? "full" : "half"
}

/** KPIs leave the grid for the strip at the top; everything else is body. */
export function splitPanels(panels: DashboardPanel[]): {
  kpis: DashboardPanel[]
  body: DashboardPanel[]
} {
  const kpis: DashboardPanel[] = []
  const body: DashboardPanel[] = []
  for (const panel of panels) {
    ;(isKpiPanel(panel) ? kpis : body).push(panel)
  }
  return { kpis, body }
}

type Indexed = { panel: DashboardPanel; index: number }

/**
 * Order panels so no row is left half empty, for dashboards with no stored spans.
 *
 * Single-step lookahead rather than a sort: when a slot is open and the next
 * panel cannot fill it, the nearest half-width panel is pulled forward. That
 * removes every avoidable hole while moving as few panels as possible, so
 * related charts stay near each other and the planner's intended order mostly
 * survives. Sorting by width would pack perfectly and scatter the meaning.
 *
 * Tables sink to the bottom: they are reference material, and a full-width
 * table in the middle cuts the dashboard in half.
 */
function packDerived(items: Indexed[]): LaidOutPanel[] {
  const isTable = (p: DashboardPanel) => p.display === "table" || !p.chart_type
  const queue = [
    ...items.filter((it) => !isTable(it.panel)),
    ...items.filter((it) => isTable(it.panel)),
  ]

  const out: LaidOutPanel[] = []
  // Index in `out` of a half-width panel sitting alone on the left of a row.
  let openSlot: number | null = null

  /* Nothing can share this panel's row, so give it the whole row. A lone panel
     spanning the width reads as deliberate; a lone panel beside a gap reads as
     a bug. Circles are the exception — a full-width pie is all margin, and the
     gap is the better of the two. */
  const absorbRow = (at: number) => {
    const type = out[at].panel.chart_type
    if (type === "pie" || type === "donut" || type === "doughnut") return
    out[at].span = GRID_COLUMNS
  }

  while (queue.length > 0) {
    if (openSlot !== null) {
      const i = queue.findIndex((it) => panelWidth(it.panel) === "half")
      if (i >= 0) {
        const next = queue.splice(i, 1)[0]
        out.push({
          panel: next.panel,
          index: next.index,
          span: 2,
          rowSpan: DEFAULT_ROW_SPAN,
        })
        openSlot = null
        continue
      }
      absorbRow(openSlot)
      openSlot = null
    }

    const next = queue.shift()!
    const span = chartSpan(next.panel, null)
    out.push({ panel: next.panel, index: next.index, span, rowSpan: DEFAULT_ROW_SPAN })
    openSlot = span === MIN_CHART_SPAN ? out.length - 1 : null
  }

  // A half-width panel left open at the very end has the same problem.
  if (openSlot !== null) absorbRow(openSlot)

  return out
}

/**
 * Lay out one section's body panels (charts / tables — never KPIs).
 *
 * All-or-nothing per section: if ANY panel carries a stored span, every panel
 * in that section is placed from stored values (a missing one falling back to
 * its derived width). Mixing a packed run with fixed spans would move panels
 * the editor had deliberately placed.
 *
 * Stored quarter-widths are upgraded to half so older Arrange saves do not
 * keep showing cramped 4-up rows.
 */
export function layoutSection(items: Indexed[]): LaidOutPanel[] {
  const body = items.filter((it) => !isKpiPanel(it.panel))
  const hasStored = body.some(
    (it) => clampSpan((it.panel as { span?: unknown }).span) !== null
  )
  if (!hasStored) return packDerived(body)

  return body.map((it) => {
    const raw = it.panel as { span?: unknown; row_span?: unknown }
    return {
      panel: it.panel,
      index: it.index,
      span: chartSpan(it.panel, raw.span),
      rowSpan: clampRowSpan(raw.row_span) ?? DEFAULT_ROW_SPAN,
    }
  })
}

/**
 * Group panels into tabs, preserving the order the planner emitted them in.
 *
 * A dashboard whose panels carry no section — every one saved before sections
 * existed — comes back as a single unnamed section, which the page renders
 * without a tab bar. One tab is not a tab.
 */
export function groupBySection(panels: DashboardPanel[]): DashboardSection[] {
  const order: (string | null)[] = []
  const buckets = new Map<string | null, Indexed[]>()

  panels.forEach((panel, index) => {
    const raw = (panel as { section?: unknown }).section
    const name = typeof raw === "string" && raw.trim() ? raw.trim() : null
    if (!buckets.has(name)) {
      buckets.set(name, [])
      order.push(name)
    }
    buckets.get(name)!.push({ panel, index })
  })

  // A dashboard where only SOME panels were given a section would otherwise
  // show an untitled tab beside named ones. Those strays join the first named
  // section rather than forming a nameless tab of their own.
  const named = order.filter((n) => n !== null) as string[]
  if (named.length > 0 && buckets.has(null)) {
    const strays = buckets.get(null)!
    const first = buckets.get(named[0])!
    buckets.set(named[0], [...first, ...strays])
    buckets.delete(null)
    order.splice(order.indexOf(null), 1)
  }

  return order.map((name) => {
    const items = buckets.get(name)!
    return {
      name,
      kpis: items.filter((it) => isKpiPanel(it.panel)).map((it) => it.panel),
      panels: layoutSection(items),
    }
  })
}

/**
 * Tailwind column-span classes, written out in full so the JIT compiler sees
 * them — a class assembled from a template string is never emitted.
 *
 * The grid is 4 columns only at its widest. At the middle width it is 2, where
 * a quarter and a half both become one column, and three-quarters and full both
 * become two. Below that everything stacks.
 */
export const SPAN_CLASS: Record<PanelSpan, string> = {
  1: "@xl/panels:col-span-1 @3xl/panels:col-span-1",
  2: "@xl/panels:col-span-1 @3xl/panels:col-span-2",
  3: "@xl/panels:col-span-2 @3xl/panels:col-span-3",
  4: "@xl/panels:col-span-2 @3xl/panels:col-span-4",
}

/**
 * How much of its natural height a panel's CONTENT gets at each step.
 *
 * Applied to the chart's computed pixel height and to a table's scroll cap,
 * because that is the only thing that actually changes a panel's size — the
 * wrapper cannot squeeze a chart that was handed an explicit height. A panel is
 * then as tall as its content, so no wrapper min-height is needed and rows are
 * never padded with empty space.
 */
export const ROW_SPAN_SCALE: Record<PanelRowSpan, number> = {
  1: 0.62,
  2: 1,
  3: 1.5,
  4: 2.1,
}

/** The content scale for a panel, from its stored row_span. */
export function rowSpanScale(panel: {
  row_span?: number | null
}): number {
  return ROW_SPAN_SCALE[clampRowSpan(panel.row_span) ?? DEFAULT_ROW_SPAN]
}
