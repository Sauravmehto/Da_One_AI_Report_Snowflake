import {
  formatStatValue,
  StatCard,
} from "@/components/report/stat-card"
import type { StatWidgetConfig, WidgetRenderProps } from "@/lib/report-types"

export function StatWidget({ widget }: WidgetRenderProps) {
  const w = widget as StatWidgetConfig
  return (
    <StatCard
      description={w.title}
      value={formatStatValue(w.value, w.format ?? "plain")}
      trend={w.trend}
      footerTitle={w.footerTitle}
      footerDescription={w.footerDescription}
    />
  )
}
