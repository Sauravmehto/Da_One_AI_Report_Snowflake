import {
  DefaultZIndexes,
  usePlotArea,
  useXAxisScale,
  useYAxisScale,
  ZIndexLayer,
} from "recharts"

import {
  extremes,
  formatLabelValue,
  LABEL_ROOM,
  labelIndices,
  labelWidth,
  MARKER_R,
  MARKER_STROKE,
  markerDot,
  placeLabels,
  textOn,
  type AxisFormat,
  type DataLabels,
  type LabelBox,
  type LabelCandidate,
  type LabelPosition,
  type LabelSpot,
  type MarkerMode,
  type Unit,
} from "@/lib/chart-standard"

/** One series the labels are written for. */
export type LabelSeries = {
  key: string
  color: string
  fmt: AxisFormat
  /** A point on a line or area, a bar of its own, or a segment of a stack. */
  shape: "point" | "bar" | "stack"
  /** Which of its values this series labels. */
  labels: DataLabels
  yAxisId?: string | number
  /** Which bar in a category's group this series is, from 0. */
  barSlot?: number
  /** The axis title names the currency, so the labels leave it out. */
  bare?: boolean
  /** Points only: which of them carry a dot. */
  markers?: MarkerMode
  /** Bars only: where the value sits. */
  position?: LabelPosition
}

/* Recharts' own defaults for spacing bars, which the labels must match to
   sit over the bars rather than beside them. */
const BAR_GAP = 4
const BAR_CATEGORY_GAP = 0.1

/** A marked point's reach, its ring included. */
const POINT_R = MARKER_R + MARKER_STROKE / 2 + 0.5

/** How far apart the samples along a line are, and how thick each one is. */
const LINE_STEP_PX = 6
const LINE_HALF_PX = 2

/**
 * Where one bar sits within its category band — the same arithmetic Recharts
 * uses to draw it.
 */
function barSpan(bandStart: number, bandSize: number, slots: number, slot: number, maxBarSize?: number) {
  const offset = bandSize * BAR_CATEGORY_GAP
  const gap = bandSize - 2 * offset - (slots - 1) * BAR_GAP <= 0 ? 0 : BAR_GAP
  let original = (bandSize - 2 * offset - (slots - 1) * gap) / slots
  if (original > 1) original = Math.floor(original)
  const size = maxBarSize !== undefined ? Math.min(original, maxBarSize) : original
  const start = bandStart + offset + (original + gap) * slot + (original - size) / 2
  return { start, size }
}

/** Boxes along the straight run between two points, so labels can keep off the line. */
function lineBoxes(a: { px: number; py: number }, b: { px: number; py: number }, owner: number): LabelBox[] {
  const length = Math.hypot(b.px - a.px, b.py - a.py)
  const steps = Math.max(1, Math.ceil(length / LINE_STEP_PX))
  const out: LabelBox[] = []
  for (let n = 0; n <= steps; n++) {
    const x = a.px + ((b.px - a.px) * n) / steps
    const y = a.py + ((b.py - a.py) * n) / steps
    out.push({
      x0: x - LINE_HALF_PX, x1: x + LINE_HALF_PX, y0: y - LINE_HALF_PX, y1: y + LINE_HALF_PX,
      owner, soft: true,
    })
  }
  return out
}

/** Spots for a bar's value, in order of preference, for the chosen position. */
function barSpots(
  position: LabelPosition,
  {
    horizontal,
    negative,
    wide,
    length = 0,
  }: { horizontal: boolean; negative: boolean; wide: boolean; length?: number }
): LabelSpot[] {
  const sign = negative ? -1 : 1
  let outside: LabelSpot[]
  let inside: LabelSpot[]
  if (horizontal) {
    outside = [{ dx: 6 * sign, dy: 0, anchor: negative ? "end" : "start" }]
    inside = [{ dx: -6 * sign, dy: 0, anchor: negative ? "start" : "end", inside: true }]
  } else if (wide) {
    // Wider than its bar, the label would lie across the next bar in the
    // group, so it stands upright over its own.
    outside = [{ dx: 0, dy: -5 * sign, anchor: negative ? "end" : "start", rotate: true }]
    inside = [{ dx: 0, dy: 5 * sign, anchor: negative ? "start" : "end", rotate: true, inside: true }]
  } else {
    outside = [{ dx: 0, dy: negative ? 11 : -10, anchor: "middle" }]
    inside = [{ dx: 0, dy: negative ? -10 : 11, anchor: "middle", inside: true }]
  }
  // Last resort before leaving a bar unlabelled: the middle of the bar, clear
  // of a line crossing its end.
  const back = -Math.sign(negative ? -1 : 1) * (length / 2)
  const centre: LabelSpot[] = horizontal
    ? [{ dx: back, dy: 0, anchor: "middle", inside: true }]
    : [{ dx: 0, dy: -back, anchor: "middle", rotate: wide, inside: true }]
  if (position === "outside_end") return outside
  if (position === "inside_end") return [...inside, ...outside, ...centre]
  return [...outside, ...inside, ...centre]
}

/**
 * Value labels for a cartesian chart, placed so none overlaps another.
 *
 * Rendered as a child of the chart, where the axis scales give every label its
 * pixel position. All series are placed together: that is what lets a label on
 * one line step aside for a label on the other instead of printing over it.
 * Labels also keep off the other series' lines, marked points and bars.
 */
export function ValueLabels({
  data,
  categoryKey,
  series,
  unit,
  horizontal = false,
  barSlots = 1,
  maxBarSize,
  valueDomain,
  room = LABEL_ROOM,
  stackTotals,
}: {
  data: ReadonlyArray<Record<string, unknown>>
  categoryKey: string
  series: readonly LabelSeries[]
  /** The panel's number unit, which every label uses when set. */
  unit?: Unit
  /** Bars lying on their side: categories down the y axis. */
  horizontal?: boolean
  /** How many bars stand side by side in one category. */
  barSlots?: number
  maxBarSize?: number
  /** The bars' value axis range; a bar starting below it starts at its edge. */
  valueDomain?: readonly [number, number]
  /** Room above the plot the chart left for labels. */
  room?: number
  /** Write each stack's total at its end, in this format. */
  stackTotals?: { fmt: AxisFormat; bare?: boolean }
}) {
  const plot = usePlotArea()
  const xScale = useXAxisScale()
  const firstAxis = series[0]?.yAxisId
  const yScale = useYAxisScale(firstAxis)
  const rightScale = useYAxisScale(series.find((s) => s.yAxisId !== firstAxis)?.yAxisId ?? firstAxis)
  const anything = series.some((s) => s.labels !== "none") || Boolean(stackTotals)
  if (!anything || !plot || !xScale || !yScale || data.length === 0) return null

  const valueScaleFor = (s: LabelSeries) =>
    horizontal ? xScale : s.yAxisId !== undefined && s.yAxisId !== firstAxis ? rightScale : yScale
  const categoryScale = horizontal ? yScale : xScale
  const [lo, hi] = valueDomain ?? [-Infinity, Infinity]
  const clamp = (v: number) => Math.min(Math.max(v, Math.min(lo, hi)), Math.max(lo, hi))

  const candidates: LabelCandidate[] = []
  const meta: Array<{ series?: LabelSeries; px: number; py: number }> = []
  const obstacles: LabelBox[] = []
  const stacks = new Map<number, { pos: number; neg: number; total: number; start: number; size: number }>()

  series.forEach((s, owner) => {
    const valueScale = valueScaleFor(s)
    if (!valueScale) return
    const values = data.map((row) => row[s.key])
    const chosen = new Set(labelIndices(values, s.labels))
    // With every point labelled, the extremes still go first so they are
    // never the ones crowded out.
    const firsts = new Set(s.labels === "all" ? labelIndices(values, "extremes") : chosen)
    const lowest = extremes(values)
    // One decimal convention per series: 7% beside 11.5% reads as a different kind of number.
    const fractional = values.some((v) => typeof v === "number" && !Number.isInteger(v))
    let previous: { px: number; py: number } | undefined

    /* One orientation per series. Deciding bar by bar stood some labels
       upright and left others flat on the same chart. If any value is wider
       than its bar, every label of the series stands upright. */
    let upright = false
    if (s.shape === "bar" && !horizontal) {
      const first = data.find((row) => typeof row[s.key] === "number")
      const bandStart = first ? categoryScale?.(first[categoryKey], { position: "start" }) : undefined
      const bandEnd = first ? categoryScale?.(first[categoryKey], { position: "end" }) : undefined
      if (bandStart !== undefined && bandEnd !== undefined) {
        const { size } = barSpan(bandStart, bandEnd - bandStart, barSlots, s.barSlot ?? owner, maxBarSize)
        upright = values.some(
          (v) =>
            typeof v === "number" &&
            Number.isFinite(v) &&
            labelWidth(formatLabelValue(v, s.fmt, { unit, currencyInTitle: s.bare, fractional })) > size + 4
        )
      }
    }

    data.forEach((row, i) => {
      const value = row[s.key]
      if (typeof value !== "number" || !Number.isFinite(value)) return
      const category = row[categoryKey] as string | number
      const text = formatLabelValue(value, s.fmt, { unit, currencyInTitle: s.bare, fractional })

      if (s.shape === "point") {
        const px = categoryScale?.(category, { position: "middle" })
        const py = valueScale(value)
        if (px === undefined || py === undefined) return
        const point = { px, py }
        if (previous) obstacles.push(...lineBoxes(previous, point, owner))
        previous = point
        const marked = s.markers === "all" || (s.markers === "labelled" && chosen.has(i))
        if (marked) {
          obstacles.push({ x0: px - POINT_R, x1: px + POINT_R, y0: py - POINT_R, y1: py + POINT_R })
        }
        if (!chosen.has(i)) return
        const above: LabelSpot[] = [
          { dx: 0, dy: -12, anchor: "middle" },
          { dx: -5, dy: -12, anchor: "end" },
          { dx: 5, dy: -12, anchor: "start" },
        ]
        const below: LabelSpot[] = above.map((spot) => ({ ...spot, dy: 13 }))
        // A low point's label goes under it, where the line is not.
        const lowFirst =
          s.labels === "min" ||
          (s.labels !== "max" && lowest !== undefined && i === lowest.min && lowest.min !== lowest.max)
        candidates.push({
          text,
          x: px,
          y: py,
          spots: lowFirst ? [...below, ...above] : [...above, ...below],
          priority: firsts.has(i) ? 0 : 1,
          owner,
          overBars: true,
        })
        meta.push({ series: s, px, py })
        return
      }

      const bandStart = categoryScale?.(category, { position: "start" })
      const bandEnd = categoryScale?.(category, { position: "end" })
      if (bandStart === undefined || bandEnd === undefined) return
      const slot = s.shape === "stack" ? 0 : s.barSlot ?? owner
      const slots = s.shape === "stack" ? 1 : barSlots
      const { start, size } = barSpan(bandStart, bandEnd - bandStart, slots, slot, maxBarSize)

      // A stack draws each segment from where the last one ended.
      let from = 0
      let to = value
      if (s.shape === "stack") {
        const base = stacks.get(i) ?? { pos: 0, neg: 0, total: 0, start, size }
        from = value >= 0 ? base.pos : base.neg
        to = from + value
        stacks.set(i, {
          ...base,
          pos: value >= 0 ? to : base.pos,
          neg: value < 0 ? to : base.neg,
          total: base.total + value,
        })
      }
      const p0 = valueScale(clamp(from))
      const p1 = valueScale(clamp(to))
      if (p0 === undefined || p1 === undefined) return

      const rect: LabelBox = horizontal
        ? { x0: Math.min(p0, p1), x1: Math.max(p0, p1), y0: start, y1: start + size }
        : { x0: start, x1: start + size, y0: Math.min(p0, p1), y1: Math.max(p0, p1) }
      // Owned, so the bar blocks every label but its own.
      if (s.shape === "bar") obstacles.push({ ...rect, owner, bar: true })
      if (!chosen.has(i)) return

      const mid = start + size / 2
      const segment = s.shape === "stack"
      const x = segment ? (horizontal ? (p0 + p1) / 2 : mid) : horizontal ? p1 : mid
      const y = segment ? (horizontal ? mid : (p0 + p1) / 2) : horizontal ? mid : p1
      candidates.push({
        text,
        x,
        y,
        spots: segment
          ? [{ dx: 0, dy: 0, anchor: "middle", inside: true }]
          : barSpots(s.position ?? "auto", {
              horizontal,
              negative: value < 0,
              wide: upright,
              length: Math.abs(p1 - p0),
            }),
        priority: firsts.has(i) ? 0 : 1,
        within: rect,
        fontSize: segment ? 10 : 11,
        owner,
      })
      meta.push({ series: s, px: x, py: y })
    })
  })

  // Each stack's total, at the end of its positive run. Placed before the
  // segments' own values so a crowded chart keeps its totals.
  if (stackTotals) {
    const valueScale = horizontal ? xScale : yScale
    const totals = [...stacks.values()].map((stack) => stack.total)
    const fractional = totals.some((v) => !Number.isInteger(v))
    for (const stack of stacks.values()) {
      const end = valueScale(clamp(stack.total >= 0 ? stack.pos : stack.neg))
      if (end === undefined) continue
      const text = formatLabelValue(stack.total, stackTotals.fmt, {
        unit, currencyInTitle: stackTotals.bare, fractional,
      })
      const mid = stack.start + stack.size / 2
      candidates.push({
        text,
        x: horizontal ? end : mid,
        y: horizontal ? mid : end,
        spots: barSpots("outside_end", {
          horizontal,
          negative: stack.total < 0,
          wide: !horizontal && labelWidth(text) > stack.size + 4,
        }),
        priority: -1,
      })
      meta.push({ px: horizontal ? end : mid, py: horizontal ? mid : end })
    }
  }

  const bounds: LabelBox = {
    x0: plot.x - 2,
    x1: plot.x + plot.width + (horizontal ? 60 : 2),
    y0: plot.y - Math.max(room, 4),
    y1: plot.y + plot.height,
  }
  const placed = placeLabels(candidates, bounds, obstacles)

  return (
    <ZIndexLayer zIndex={DefaultZIndexes.label}>
      <g className="value-labels" pointerEvents="none">
        {placed.map((label) => {
          // A labelled point on a line without dots of its own still gets
          // one, so the value is tied to the point it belongs to.
          const m = meta[label.index]
          return m.series?.shape === "point" && m.series.markers === "labelled" ? (
            <circle key={`dot-${label.index}`} cx={m.px} cy={m.py} {...markerDot(m.series.color)} />
          ) : null
        })}
        {placed.map((label) => {
          const m = meta[label.index]
          const candidate = candidates[label.index]
          const onShape = label.inside && m.series !== undefined && m.series.shape !== "point"
          const shapeColor = m.series?.color ?? "var(--background)"
          return (
            <text
              key={label.index}
              x={label.x}
              y={label.y}
              transform={label.rotate ? `rotate(-90 ${label.x} ${label.y})` : undefined}
              textAnchor={label.anchor}
              dominantBaseline="central"
              fontSize={candidate.fontSize ?? 11}
              fontWeight={600}
              fill={onShape ? textOn(shapeColor) : "var(--foreground)"}
              // A halo in the colour behind the text, so it stays legible where
              // it crosses a grid rule or the edge of its bar.
              stroke={onShape ? shapeColor : "var(--background)"}
              strokeWidth={3}
              strokeLinejoin="round"
              paintOrder="stroke"
              style={{ fontVariantNumeric: "tabular-nums" }}
            >
              {candidate.text}
            </text>
          )
        })}
      </g>
    </ZIndexLayer>
  )
}
