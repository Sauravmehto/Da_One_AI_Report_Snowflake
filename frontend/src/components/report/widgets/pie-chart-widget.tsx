import { Cell, Pie, PieChart } from "recharts"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { buildChartConfig } from "@/lib/build-chart-config"
import { sequentialColorsForData } from "@/lib/sequential-color"
import type { PieWidgetConfig, WidgetRenderProps } from "@/lib/report-types"

function formatLabel(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

export function PieChartWidget({ widget }: WidgetRenderProps) {
  const w = widget as PieWidgetConfig
  const { x, y } = w.encoding
  const valueKey = y[0]
  const isDonut = w.modifiers?.variant === "donut"
  const isSequential = w.modifiers?.colorScale === "sequential"
  const sequentialColors = isSequential
    ? sequentialColorsForData(w.data, valueKey)
    : null

  const chartConfig: ChartConfig = isSequential
    ? Object.fromEntries(
        w.data.map((entry, i) => {
          const category = String(entry[x])
          return [
            category,
            {
              label: formatLabel(category),
              color: sequentialColors![i],
            },
          ]
        })
      )
    : buildChartConfig([valueKey])

  return (
    <Card className="flex flex-col">
      <CardHeader className="items-center pb-0">
        <CardTitle>{w.title}</CardTitle>
        {w.description ? (
          <CardDescription>{w.description}</CardDescription>
        ) : null}
      </CardHeader>
      <CardContent className="flex-1 pb-0">
        <ChartContainer
          config={chartConfig}
          className="mx-auto aspect-square max-h-[250px]"
        >
          <PieChart>
            <ChartTooltip
              cursor={false}
              content={<ChartTooltipContent hideLabel />}
            />
            <Pie
              data={w.data}
              dataKey={valueKey}
              nameKey={x}
              innerRadius={isDonut ? 60 : 0}
            >
              {isSequential
                ? w.data.map((_, i) => (
                    <Cell key={i} fill={sequentialColors![i]} />
                  ))
                : null}
            </Pie>
          </PieChart>
        </ChartContainer>
      </CardContent>
    </Card>
  )
}
