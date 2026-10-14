import { Bar, BarChart, CartesianGrid, Cell, XAxis, YAxis } from "recharts"

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
import { sequentialColorsForData } from "@/lib/sequential-color"
import type { BarWidgetConfig, WidgetRenderProps } from "@/lib/report-types"

export function BarChartWidget({ widget }: WidgetRenderProps) {
  const w = widget as BarWidgetConfig
  const { x, y } = w.encoding
  const orientation = w.modifiers?.orientation ?? "vertical"
  const series = w.modifiers?.series ?? (y.length > 1 ? "grouped" : "single")
  const isHorizontal = orientation === "horizontal"
  const isStacked = series === "stacked"
  const isNegative = w.modifiers?.negative ?? false
  const isSequential =
    w.modifiers?.colorScale === "sequential" && y.length === 1 && !isNegative
  const sequentialColors = isSequential
    ? sequentialColorsForData(w.data, y[0])
    : null
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
          <BarChart
            accessibilityLayer
            data={w.data}
            layout={isHorizontal ? "vertical" : "horizontal"}
            margin={isHorizontal ? { left: 0 } : undefined}
          >
            <CartesianGrid vertical={false} />
            {isHorizontal ? (
              <>
                <XAxis type="number" hide />
                <YAxis
                  dataKey={x}
                  type="category"
                  tickLine={false}
                  axisLine={false}
                  width={80}
                />
              </>
            ) : (
              <XAxis
                dataKey={x}
                tickLine={false}
                tickMargin={10}
                axisLine={false}
                tickFormatter={(value) => String(value).slice(0, 6)}
              />
            )}
            <ChartTooltip content={<ChartTooltipContent hideLabel />} />
            {y.length > 1 ? (
              <ChartLegend content={<ChartLegendContent />} />
            ) : null}
            {y.map((key, index) => (
              <Bar
                key={key}
                dataKey={key}
                stackId={isStacked ? "a" : undefined}
                fill={`var(--color-${key})`}
                radius={
                  isStacked
                    ? index === 0
                      ? [0, 0, 4, 4]
                      : [4, 4, 0, 0]
                    : 4
                }
              >
                {isNegative && y.length === 1
                  ? w.data.map((entry, i) => (
                      <Cell
                        key={i}
                        fill={
                          Number(entry[key]) >= 0
                            ? "var(--chart-1)"
                            : "var(--chart-2)"
                        }
                      />
                    ))
                  : isSequential && key === y[0]
                    ? w.data.map((_, i) => (
                        <Cell key={i} fill={sequentialColors![i]} />
                      ))
                    : null}
              </Bar>
            ))}
          </BarChart>
        </ChartContainer>
      </CardContent>
    </Card>
  )
}
