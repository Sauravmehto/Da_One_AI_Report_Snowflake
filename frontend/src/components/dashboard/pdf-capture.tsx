/**
 * What the PDF export takes from the page: a picture of every chart, and every
 * table written the way the dashboard writes it.
 *
 * The PDF used to redraw charts itself, in Python, and every chart setting
 * added here (value labels, markers, reference lines, bubble sizes, axis
 * bounds, readable titles) had to be built a second time there or went
 * missing from the file. A picture of the chart as the dashboard draws it
 * cannot disagree with the dashboard.
 *
 * Charts are drawn OFF SCREEN for the picture, all of them. A dashboard mounts
 * one tab at a time, and taking pictures of what happened to be mounted left
 * every chart on another tab without one, which is why this was abandoned
 * before. The server still draws any chart that arrives without a picture.
 */

import { createRoot } from "react-dom/client"

import {
  ChartDisplayContext,
  dashboardCurrencyUnit,
} from "@/components/dashboard/chart-display-context"
import { PanelChart, type DashboardPanel } from "@/components/dashboard/dashboard-panel"
import {
  columnDateGrains,
  formatCell,
  isNumericColumn,
  seriesLabel,
} from "@/lib/dashboard-format"
import { elementToPng } from "@/lib/panel-image"

/** A table as the dashboard shows it: readable headers, formatted cells. */
export type PdfTable = {
  headers: string[]
  /** Right-aligned columns. */
  numeric: boolean[]
  rows: string[][]
}

export type PdfCapture = {
  /** {panel title: PNG data URL} */
  images: Record<string, string>
  /** {panel title: table} */
  tables: Record<string, PdfTable>
}

/** Rows a PDF table draws; matches _MAX_PDF_TABLE_ROWS on the server. */
const PDF_TABLE_ROWS = 500

/**
 * The width charts are drawn at for their picture. Near the PDF's own content
 * width in points, so text lands at a readable size once placed.
 */
const CAPTURE_WIDTH = 720

/** Longest a capture waits for charts to finish drawing, in ms. */
const DRAW_TIMEOUT_MS = 8000

function isKpi(panel: DashboardPanel): boolean {
  return (panel.chart_type ?? "") === "kpi" || panel.display === "kpi"
}

/** Whether the dashboard draws this panel as a chart. Mirrors DashboardPanelCard. */
function drawsChart(panel: DashboardPanel): boolean {
  const type = panel.chart_type ?? ""
  return Boolean(type) && type !== "table" && !isKpi(panel) && !panel.error && panel.rows.length > 0
}

function tableOf(panel: DashboardPanel): PdfTable {
  const { columns, rows } = panel
  const grains = columnDateGrains(columns, rows)
  return {
    headers: columns.map((column) => seriesLabel(column, panel.column_formats)),
    numeric: columns.map((_, i) => isNumericColumn(rows, i)),
    rows: rows.slice(0, PDF_TABLE_ROWS).map((row) =>
      columns.map((column, i) =>
        formatCell(column, row[i], panel.column_formats, grains[i], panel.default_currency)
      )
    ),
  }
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

/** The area of the largest SVG inside a node, in px². */
function largestSvgArea(node: Element): number {
  let best = 0
  for (const svg of node.querySelectorAll("svg")) {
    const box = svg.getBoundingClientRect()
    best = Math.max(best, box.width * box.height)
  }
  return best
}

/** Whether a chart has finished drawing: a heatmap is HTML; a map waits for its outlines. */
function drawn(node: Element | null, panel: DashboardPanel): boolean {
  if (!node) return false
  const type = panel.chart_type ?? ""
  if (type === "heatmap") return (node as HTMLElement).offsetHeight > 40
  if (largestSvgArea(node) < 100 * 100) return false
  if (type === "geo_countries" || type === "geo_us_states") {
    return node.querySelectorAll("path").length >= 5
  }
  return true
}

/**
 * Pictures and tables for the PDF export.
 *
 * Pass every panel of the dashboard, from every tab. A chart that fails to
 * draw in time or to become a picture is left out, and the server draws it.
 */
export async function captureDashboardForPdf(panels: DashboardPanel[]): Promise<PdfCapture> {
  const tables: Record<string, PdfTable> = {}
  for (const panel of panels) {
    if (!isKpi(panel) && !panel.error && panel.rows.length > 0) {
      tables[panel.title] = tableOf(panel)
    }
  }

  const charts = panels.filter(drawsChart)
  const images: Record<string, string> = {}
  if (charts.length === 0) return { images, tables }

  const host = document.createElement("div")
  // Light theme whatever the page is in, and out of sight while drawing.
  host.className = "pdf-capture bg-background text-foreground"
  host.setAttribute("aria-hidden", "true")
  host.style.cssText = `position:fixed;left:-100000px;top:0;width:${CAPTURE_WIDTH}px;pointer-events:none;`
  document.body.appendChild(host)
  const root = createRoot(host)

  try {
    root.render(
      <ChartDisplayContext.Provider value={{ currencyUnit: dashboardCurrencyUnit(panels) }}>
        {charts.map((panel, i) => (
          <div
            key={`${i}-${panel.title}`}
            data-capture={i}
            className="bg-background px-3 py-2"
            // A printed heatmap is as wide as its blocks of columns, which can
            // be wider than a chart; the PDF scales the picture to the page.
            style={
              panel.chart_type === "heatmap"
                ? { width: "max-content", minWidth: CAPTURE_WIDTH }
                : { width: CAPTURE_WIDTH }
            }
          >
            <PanelChart panel={panel} print />
          </div>
        ))}
      </ChartDisplayContext.Provider>
    )

    // Charts measure their width and redraw once it lands, and value labels
    // are placed after the axes exist, so readiness is polled rather than
    // assumed after one frame.
    const deadline = performance.now() + DRAW_TIMEOUT_MS
    while (performance.now() < deadline) {
      await wait(120)
      const ready = charts.every((panel, i) => drawn(host.querySelector(`[data-capture="${i}"]`), panel))
      if (ready) break
    }
    await wait(250)

    for (const [i, panel] of charts.entries()) {
      const node = host.querySelector<HTMLElement>(`[data-capture="${i}"]`)
      if (!node || !drawn(node, panel)) continue
      const png = await elementToPng(node)
      if (png) images[panel.title] = png
    }
  } finally {
    root.unmount()
    host.remove()
  }
  return { images, tables }
}
