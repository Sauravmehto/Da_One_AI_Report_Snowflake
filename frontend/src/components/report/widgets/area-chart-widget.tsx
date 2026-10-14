import { Area, AreaChart, CartesianGrid, XAxis } from "recharts"

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
import type { AreaWidgetConfig, WidgetRenderProps } from "@/lib/report-types"

function areaType(curve?: string): "linear" | "natural" | "step" {
  if (curve === "linear" || curve === "step") return curve
  return "natural"
}

export function AreaChartWidget({ widget }: WidgetRenderProps) {
  const w = widget as AreaWidgetConfig
  const { x, y } = w.encoding
  const isStacked = w.modifiers?.series === "stacked"
  const stackOffset =
    w.modifiers?.stackOffset === "expand" ? "expand" : undefined
  const curve = areaType(w.modifiers?.curve)
  const chartConfig = buildChartConfig(y)

  return (
    <Card>
      <CardHeader>
        <CardTitle>{w.title}</CardTitle>
        {w.description ? (
          <CardDescription>{w.description}</CardDescription>
        ) : null}
      </CardHeader>
      <CardContent>
        <ChartContainer config={chartConfig}>
          <AreaChart
            accessibilityLayer
            data={w.data}
            stackOffset={isStacked ? stackOffset : undefined}
          >
            <CartesianGrid vertical={false} />
            <XAxis
              dataKey={x}
              tickLine={false}
              axisLine={false}
              tickMargin={8}
              tickFormatter={(value) => String(value).slice(0, 6)}
            />
            <ChartTooltip content={<ChartTooltipContent />} />
            {y.length > 1 ? (
              <ChartLegend content={<ChartLegendContent />} />
            ) : null}
            {y.map((key) => (
              <Area
                key={key}
                type={curve}
                dataKey={key}
                stackId={isStacked ? "a" : undefined}
                fill={`var(--color-${key})`}
                stroke={`var(--color-${key})`}
                fillOpacity={0.4}
              />
            ))}
          </AreaChart>
        </ChartContainer>
      </CardContent>
    </Card>
  )
}
