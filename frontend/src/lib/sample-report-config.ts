import { CHART_GALLERY } from "@/lib/chart-gallery-config"
import type { ChartGalleryEntry, ReportConfig, WidgetLayout } from "@/lib/report-types"

function layoutForGalleryEntry(entry: ChartGalleryEntry): WidgetLayout {
  if (entry.fullWidth) {
    return { colSpan: 4 }
  }
  if (entry.widget.type === "stat") {
    return { colSpan: 1 }
  }
  return { colSpan: 2 }
}

/** Report-shaped view of the chart gallery — for ReportRenderer demos / API reference. */
export const SAMPLE_REPORT_CONFIG: ReportConfig = {
  version: 1,
  title: "Chart Gallery",
  widgets: CHART_GALLERY.map((entry) => ({
    ...entry.widget,
    layout: layoutForGalleryEntry(entry),
  })),
}
