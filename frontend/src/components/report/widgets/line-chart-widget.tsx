import { CartesianGrid, Line, LineChart, XAxis } from "recharts"

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
import type { LineWidgetConfig, WidgetRenderProps } from "@/lib/report-types"

function lineType(curve?: string): "linear" | "natural" | "step" {
  if (curve === "linear" || curve === "step") return curve
  return "natural"
}

export function LineChartWidget({ widget }: WidgetRenderProps) {
  const w = widget as LineWidgetConfig
  const { x, y } = w.encoding
  const curve = lineType(w.modifiers?.curve)
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
          <LineChart accessibilityLayer data={w.data}>
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
              <Line
                key={key}
                type={curve}
                dataKey={key}
                stroke={`var(--color-${key})`}
                strokeWidth={2}
                dot={false}
              />
            ))}
          </LineChart>
        </ChartContainer>
      </CardContent>
    </Card>
  )
}
