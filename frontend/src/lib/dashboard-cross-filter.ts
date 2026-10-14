/**
 * Client-side cross-filter: click a category on a bar/donut and keep rows
 * that share that value in any column. Panels with no matching rows stay
 * unchanged so unrelated charts do not go blank.
 */

import type { DashboardPanel } from "@/components/dashboard/dashboard-panel"
import { isKpiPanel } from "@/lib/dashboard-layout"

export type CrossFilter = {
  value: string
  /** Panel that set the filter — used for UI chrome only. */
  sourceTitle?: string
} | null

export function applyCrossFilter(
  panel: DashboardPanel,
  filter: CrossFilter
): DashboardPanel {
  if (!filter?.value) return panel
  if (isKpiPanel(panel)) return panel
  if (panel.error || panel.rows.length === 0) return panel

  const needle = filter.value
  const filtered = panel.rows.filter((row) =>
    row.some((cell) => cell !== null && cell !== undefined && String(cell) === needle)
  )
  if (filtered.length === 0) return panel
  return {
    ...panel,
    rows: filtered,
    row_count: filtered.length,
  }
}

export function applyCrossFilterAll(
  panels: DashboardPanel[],
  filter: CrossFilter
): DashboardPanel[] {
  if (!filter) return panels
  return panels.map((p) => applyCrossFilter(p, filter))
}
