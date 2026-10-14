import { cn } from "@/lib/utils"
import type { ReportConfig, WidgetLayout } from "@/lib/report-types"

import { renderWidget } from "@/components/report/widget-registry"

type ReportRendererProps = {
  config: ReportConfig
  className?: string
}

function colSpanClass(layout?: WidgetLayout): string {
  const span = layout?.colSpan ?? 1
  switch (span) {
    case 4:
      return "xl:col-span-4"
    case 3:
      return "xl:col-span-3"
    case 2:
      return "md:col-span-2"
    default:
      return "col-span-1"
  }
}

export function ReportRenderer({ config, className }: ReportRendererProps) {
  return (
    <div className={cn("flex min-h-0 flex-1 flex-col gap-6", className)}>
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">{config.title}</h1>
      </header>
      <div className="grid grid-cols-1 gap-6 md:grid-cols-2 xl:grid-cols-4">
        {config.widgets.map((widget) => (
          <div
            key={widget.id}
            className={cn("min-w-0", colSpanClass(widget.layout))}
          >
            {renderWidget({ widget })}
          </div>
        ))}
      </div>
    </div>
  )
}
