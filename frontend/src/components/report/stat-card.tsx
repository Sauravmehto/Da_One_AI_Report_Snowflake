import * as React from "react"
import { Minus, TrendingDown, TrendingUp, type LucideIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardAction,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { cn } from "@/lib/utils"

export type StatFormat = "currency" | "number" | "percent" | "plain"

export type StatTrendDirection = "up" | "down" | "neutral"

export type StatTrend = {
  value: string
  direction: StatTrendDirection
}

export type StatCardProps = {
  description: string
  value: string
  trend?: StatTrend
  footerTitle?: string
  footerDescription?: string
  className?: string
}

const trendIcons: Record<StatTrendDirection, LucideIcon> = {
  up: TrendingUp,
  down: TrendingDown,
  neutral: Minus,
}

export function formatStatValue(
  value: string | number | null | undefined,
  format: StatFormat = "plain"
): string {
  if (value === null || value === undefined || value === "") {
    return "—"
  }

  const numeric =
    typeof value === "number" ? value : Number(String(value).replace(/[%,$]/g, ""))

  if (format === "plain") {
    return String(value)
  }

  if (Number.isNaN(numeric)) {
    return String(value)
  }

  switch (format) {
    case "currency":
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
      }).format(numeric)
    case "percent":
      return new Intl.NumberFormat(undefined, {
        style: "percent",
        maximumFractionDigits: 1,
      }).format(numeric / 100)
    case "number":
      return new Intl.NumberFormat(undefined, {
        maximumFractionDigits: 2,
      }).format(numeric)
    default:
      return String(value)
  }
}

export function StatCard({
  description,
  value,
  trend,
  footerTitle,
  footerDescription,
  className,
}: StatCardProps) {
  const TrendIcon = trend ? trendIcons[trend.direction] : null
  const showFooter = Boolean(footerTitle || footerDescription)

  return (
    <Card className={cn("@container/card", className)}>
      <CardHeader>
        <CardDescription>{description}</CardDescription>
        <CardTitle className="text-2xl font-semibold tabular-nums @[250px]/card:text-3xl">
          {value}
        </CardTitle>
        {trend ? (
          <CardAction>
            <Badge variant="outline">
              {TrendIcon ? <TrendIcon /> : null}
              {trend.value}
            </Badge>
          </CardAction>
        ) : null}
      </CardHeader>
      {showFooter ? (
        <CardFooter className="flex-col items-start gap-1.5 text-sm">
          {footerTitle ? (
            <div className="line-clamp-1 flex gap-2 font-medium">
              {footerTitle}
              {TrendIcon ? <TrendIcon className="size-4" /> : null}
            </div>
          ) : null}
          {footerDescription ? (
            <div className="text-muted-foreground">{footerDescription}</div>
          ) : null}
        </CardFooter>
      ) : null}
    </Card>
  )
}

type StatCardsProps = {
  children: React.ReactNode
  className?: string
}

export function StatCards({ children, className }: StatCardsProps) {
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4 *:data-[slot=card]:bg-linear-to-t *:data-[slot=card]:from-primary/5 *:data-[slot=card]:to-card *:data-[slot=card]:shadow-xs dark:*:data-[slot=card]:bg-card",
        className
      )}
    >
      {children}
    </div>
  )
}
