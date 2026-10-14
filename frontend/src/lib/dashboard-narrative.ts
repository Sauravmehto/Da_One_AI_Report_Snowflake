/**
 * One-line narrative under the KPI band — grounded only in the headline
 * numbers already on screen, no extra model call.
 */

import type { DashboardPanel } from "@/components/dashboard/dashboard-panel"
import { columnIndex, formatKpi } from "@/lib/dashboard-format"

type KpiFact = {
  metric: string
  valueText: string
  value: number | null
  format: unknown
}

function collectKpiFacts(panels: DashboardPanel[]): KpiFact[] {
  const facts: KpiFact[] = []
  for (const panel of panels) {
    if (panel.error || panel.rows.length === 0) continue
    const { columns, rows } = panel
    const mi = columnIndex(columns, "METRIC")
    const vi = columnIndex(columns, "VALUE")
    const fi = columnIndex(columns, "FORMAT")
    const metricIdx = mi >= 0 ? mi : 0
    const valueIdx = vi >= 0 ? vi : 1
    for (const row of rows) {
      const metric = String(row[metricIdx] ?? "").trim()
      if (!metric) continue
      const raw = row[valueIdx]
      const value = typeof raw === "number" ? raw : Number(raw)
      const format = fi >= 0 ? row[fi] : null
      facts.push({
        metric,
        valueText: formatKpi(format, metric, raw, panel.default_currency),
        value: Number.isFinite(value) ? value : null,
        format,
      })
    }
  }
  return facts
}

/**
 * Build a short executive strip from KPI tiles on the active section.
 * Returns null when there is nothing useful to say.
 */
export function buildDashboardNarrative(
  kpiPanels: DashboardPanel[]
): string | null {
  const facts = collectKpiFacts(kpiPanels)
  if (facts.length === 0) return null

  const primary = facts.slice(0, 3)
  const head = primary
    .map((f) => `${f.metric} is ${f.valueText}`)
    .join("; ")

  // Optional share: overdue (or similar) as % of a total/outstanding sibling.
  const total = facts.find((f) =>
    /total|outstanding|nav|exposure/i.test(f.metric)
  )
  const part = facts.find(
    (f) =>
      f !== total &&
      /overdue|delinq|disputed|vacant|breach|watch/i.test(f.metric) &&
      f.value !== null &&
      total?.value &&
      total.value !== 0
  )
  if (part && total && part.value !== null && total.value) {
    const pct = Math.round((Math.abs(part.value) / Math.abs(total.value)) * 100)
    if (pct > 0 && pct <= 100) {
      return `${head}. ${part.metric} is ${pct}% of ${total.metric.toLowerCase()}.`
    }
  }

  if (facts.length > 3) {
    return `${head}. ${facts.length - 3} more headline metric${
      facts.length - 3 === 1 ? "" : "s"
    } on this view.`
  }
  return `${head}.`
}
