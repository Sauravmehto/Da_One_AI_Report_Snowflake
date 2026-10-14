import { PolarAngleAxis, PolarGrid, Radar, RadarChart } from "recharts"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart"
import { buildChartConfig } from "@/lib/build-chart-config"
import type { RadarWidgetConfig, WidgetRenderProps } from "@/lib/report-types"

export function RadarChartWidget({ widget }: WidgetRenderProps) {
  const w = widget as RadarWidgetConfig
  const { x, y } = w.encoding
  const chartConfig = buildChartConfig(y)

  return (
    <Card>
      <CardHeader className="items-center pb-4">
        <CardTitle>{w.title}</CardTitle>
        {w.description ? (
          <CardDescription>{w.description}</CardDescription>
        ) : null}
      </CardHeader>
      <CardContent className="pb-0">
        <ChartContainer
          config={chartConfig}
          className="mx-auto aspect-square max-h-[250px]"
        >
          <RadarChart data={w.data}>
            <ChartTooltip cursor={false} content={<ChartTooltipContent />} />
            <PolarAngleAxis dataKey={x} />
            <PolarGrid />
            {y.length > 1 ? (
              <ChartLegend content={<ChartLegendContent />} />
            ) : null}
            {y.map((key) => (
              <Radar
                key={key}
                dataKey={key}
                fill={`var(--color-${key})`}
                fillOpacity={0.6}
              />
            ))}
          </RadarChart>
        </ChartContainer>
      </CardContent>
    </Card>
  )
}
