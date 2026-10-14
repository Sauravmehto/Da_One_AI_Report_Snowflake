/**
 * Display decisions that belong to a whole dashboard rather than one chart.
 *
 * Today that is the shared currency unit: charts measured in millions on one
 * panel and billions on the next make a reader convert before comparing, and
 * no single chart can see the others to agree with them. The dashboard page
 * works the unit out once and provides it here. A chart outside any dashboard
 * — one drawn in chat — gets the empty default and keeps its own unit.
 */

import * as React from "react"

import type { DashboardPanel } from "@/components/dashboard/dashboard-panel"
import {
  extent,
  sharedCurrencyUnit,
  stackTotals,
  valueKind,
  type Unit,
} from "@/lib/chart-standard"
import { toChartData } from "@/lib/dashboard-format"

export type ChartDisplay = {
  /** The currency unit the dashboard's currency charts share, if any. */
  currencyUnit?: Unit
}

export const ChartDisplayContext = React.createContext<ChartDisplay>({})

export function useChartDisplay(): ChartDisplay {
  return React.useContext(ChartDisplayContext)
}

/** Chart types whose values are drawn against a scale a currency unit applies to. */
const SCALED_CHARTS = new Set([
  "bar", "grouped_bar", "stacked_bar", "line", "area", "combo", "area_line",
  "geo_countries", "geo_us_states",
])

/**
 * The currency unit a dashboard's currency charts share — see
 * sharedCurrencyUnit for the rule. Computed from the panels as they came
 * back from the run, so it follows the data rather than the configuration.
 */
export function dashboardCurrencyUnit(panels: readonly DashboardPanel[]): Unit | undefined {
  const maxima: number[] = []
  for (const panel of panels) {
    if (!panel.chart_type || !SCALED_CHARTS.has(panel.chart_type)) continue
    const keys = Array.isArray(panel.y) ? panel.y : panel.y ? [panel.y] : []
    if (keys.length === 0 || valueKind(keys[0], panel.column_formats) !== "currency") continue
    const rows = toChartData(panel.columns, panel.rows)
    const range =
      panel.chart_type === "stacked_bar" ? stackTotals(rows, keys) : extent(rows, keys)
    if (range) maxima.push(Math.max(Math.abs(range[0]), Math.abs(range[1])))
  }
  return sharedCurrencyUnit(maxima)
}
