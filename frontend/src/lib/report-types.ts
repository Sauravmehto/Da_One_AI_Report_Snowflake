import type { StatFormat, StatTrend } from "@/components/report/stat-card"

export type WidgetType =
  | "stat"
  | "bar"
  | "line"
  | "area"
  | "pie"
  | "radar"
  | "table"

export type WidgetLayout = {
  colSpan?: 1 | 2 | 3 | 4
  rowSpan?: 1 | 2
}

export type ChartEncoding = {
  x: string
  y: string[]
}

export type ColorScaleModifier = {
  colorScale?: "sequential"
}

export type BarModifiers = ColorScaleModifier & {
  orientation?: "vertical" | "horizontal"
  series?: "single" | "grouped" | "stacked"
  negative?: boolean
}

export type LineModifiers = {
  series?: "single" | "multiple"
  curve?: "linear" | "natural" | "step"
}

export type AreaModifiers = {
  series?: "single" | "stacked"
  curve?: "linear" | "natural" | "step"
  stackOffset?: "default" | "expand"
}

export type PieModifiers = ColorScaleModifier & {
  variant?: "pie" | "donut"
}

export type RadarModifiers = {
  series?: "single" | "multiple"
}

type BaseWidget = {
  id: string
  title: string
  description?: string
  layout?: WidgetLayout
}

export type StatWidgetConfig = BaseWidget & {
  type: "stat"
  value: number | string
  format?: StatFormat
  trend?: StatTrend
  footerTitle?: string
  footerDescription?: string
}

export type BarWidgetConfig = BaseWidget & {
  type: "bar"
  data: Record<string, string | number>[]
  encoding: ChartEncoding
  modifiers?: BarModifiers
}

export type LineWidgetConfig = BaseWidget & {
  type: "line"
  data: Record<string, string | number>[]
  encoding: ChartEncoding
  modifiers?: LineModifiers
}

export type AreaWidgetConfig = BaseWidget & {
  type: "area"
  data: Record<string, string | number>[]
  encoding: ChartEncoding
  modifiers?: AreaModifiers
}

export type PieWidgetConfig = BaseWidget & {
  type: "pie"
  data: Record<string, string | number>[]
  encoding: ChartEncoding
  modifiers?: PieModifiers
}

export type RadarWidgetConfig = BaseWidget & {
  type: "radar"
  data: Record<string, string | number>[]
  encoding: ChartEncoding
  modifiers?: RadarModifiers
}

export type TableWidgetConfig = BaseWidget & {
  type: "table"
  columns: string[]
  rows: (string | number | boolean | null)[][]
}

export type ChartWidgetConfig =
  | BarWidgetConfig
  | LineWidgetConfig
  | AreaWidgetConfig
  | PieWidgetConfig
  | RadarWidgetConfig

export type WidgetConfig =
  | StatWidgetConfig
  | ChartWidgetConfig
  | TableWidgetConfig

export type ReportConfig = {
  version: 1
  title: string
  widgets: WidgetConfig[]
}

export type ChartGalleryEntry = {
  id: string
  label: string
  section: string
  fullWidth?: boolean
  widget: WidgetConfig
}

export type WidgetRenderProps = {
  widget: WidgetConfig
}
