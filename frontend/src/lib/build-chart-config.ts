import type { ChartConfig } from "@/components/ui/chart"

const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
] as const

function formatLabel(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

export function buildChartConfig(yKeys: string[]): ChartConfig {
  const config: ChartConfig = {}
  yKeys.forEach((key, index) => {
    config[key] = {
      label: formatLabel(key),
      color: CHART_COLORS[index % CHART_COLORS.length],
    }
  })
  return config
}
