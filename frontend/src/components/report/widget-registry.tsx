import type { ComponentType, ReactElement } from "react"

import { AreaChartWidget } from "@/components/report/widgets/area-chart-widget"
import { BarChartWidget } from "@/components/report/widgets/bar-chart-widget"
import { LineChartWidget } from "@/components/report/widgets/line-chart-widget"
import { PieChartWidget } from "@/components/report/widgets/pie-chart-widget"
import { RadarChartWidget } from "@/components/report/widgets/radar-chart-widget"
import { StatWidget } from "@/components/report/widgets/stat-widget"
import { TableWidget } from "@/components/report/widgets/table-widget"
import type { WidgetRenderProps, WidgetType } from "@/lib/report-types"

export const widgetRegistry: Record<
  WidgetType,
  ComponentType<WidgetRenderProps>
> = {
  stat: StatWidget,
  bar: BarChartWidget,
  line: LineChartWidget,
  area: AreaChartWidget,
  pie: PieChartWidget,
  radar: RadarChartWidget,
  table: TableWidget,
}

export function renderWidget(props: WidgetRenderProps): ReactElement {
  const Component = widgetRegistry[props.widget.type]
  return <Component widget={props.widget} />
}
