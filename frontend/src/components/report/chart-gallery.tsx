import { ChartGridItem, ChartSection } from "@/components/charts-gallery"
import { StatCards } from "@/components/report/stat-card"
import { renderWidget } from "@/components/report/widget-registry"
import {
  CHART_GALLERY,
  CHART_GALLERY_SECTIONS,
} from "@/lib/chart-gallery-config"
import type { StatWidgetConfig } from "@/lib/report-types"

export function ChartGallery() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-10 md:gap-12">
      {CHART_GALLERY_SECTIONS.map((section) => {
        const entries = CHART_GALLERY.filter((e) => e.section === section)

        if (section === "Stat Cards") {
          return (
            <ChartSection key={section} title={section}>
              <ChartGridItem fullWidth>
                <StatCards>
                  {entries.map((entry) =>
                    renderWidget({ widget: entry.widget as StatWidgetConfig })
                  )}
                </StatCards>
              </ChartGridItem>
            </ChartSection>
          )
        }

        return (
          <ChartSection key={section} title={section}>
            {entries.map((entry) => (
              <ChartGridItem key={entry.id} fullWidth={entry.fullWidth}>
                {renderWidget({ widget: entry.widget })}
              </ChartGridItem>
            ))}
          </ChartSection>
        )
      })}
    </div>
  )
}
