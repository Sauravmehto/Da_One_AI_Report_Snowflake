/**
 * The chart display standard: one set of rules for how every chart's axes,
 * units, titles and labels read.
 *
 * Each chart used to make these choices on its own, and each got different
 * ones wrong. A line chart's fitted axis ran from 13.9M to 30.6M in steps of
 * 4.5M; a stacked bar topped out at 100.2%; a scatter fitted its y axis and
 * left x at zero, so every point sat in the right-hand quarter; legends sorted
 * alphabetically while the bars beside them did not. Nothing here draws. It
 * answers "what range, which ticks, what unit, what title" so every chart —
 * and a person overriding one in the editor — asks the same place.
 *
 * Every rule is a DEFAULT. A panel's `format` overrides it, setting by setting,
 * and an absent `format` means the standard applies unchanged.
 *
 * Imports only from dashboard-format, which imports nothing, so these rules
 * can be exercised without a browser.
 */

import {
  classify,
  currencyAffix,
  seriesLabel,
  type ColumnFormats,
} from "./dashboard-format"

/* ------------------------------------------------------------ settings */

/* The words a panel's `format` may use. Mirrored by FORMAT_CHOICES in
   backend/app/engine/dashboards.py, which drops any other value on save;
   tests/engine/test_panel_style.py holds the two lists together. */
export const UNIT_CHOICES = ["none", "K", "M", "B"] as const
export const DATA_LABELS_CHOICES = ["none", "max", "min", "extremes", "all"] as const
export const LEGEND_CHOICES = ["bottom", "right", "none"] as const
export const LABEL_POSITION_CHOICES = ["auto", "inside_end", "outside_end"] as const
export const MARKERS_CHOICES = ["auto", "show", "hide"] as const
export const REFERENCE_CHOICES = ["mean", "median", "value"] as const
export const FUNNEL_PERCENT_CHOICES = ["first", "previous", "none"] as const

export type Unit = (typeof UNIT_CHOICES)[number]
export type DataLabels = (typeof DATA_LABELS_CHOICES)[number]
export type LegendPosition = (typeof LEGEND_CHOICES)[number]
export type LabelPosition = (typeof LABEL_POSITION_CHOICES)[number]
export type Markers = (typeof MARKERS_CHOICES)[number]
export type ReferenceKind = (typeof REFERENCE_CHOICES)[number]
export type FunnelPercent = (typeof FUNNEL_PERCENT_CHOICES)[number]

/** A line across one axis: at its mean, its median, or a value typed in. */
export type AxisReference = { kind: ReferenceKind; value?: number }

/** What one series may set differently from the rest of its chart. */
export type SeriesFormat = {
  data_labels?: DataLabels
  /** Bars only. */
  label_position?: LabelPosition
  /** Lines and areas only. */
  markers?: Markers
}

/** A panel's label and axis settings. Every field absent means "standard". */
export type PanelFormat = {
  /** Title for the axis that carries the panel's x column. "" hides it. */
  x_title?: string
  /** Title for the value axis. "" hides it. */
  y_title?: string
  unit?: Unit
  /** Which values are written on the chart: per series for lines and bars,
   *  per point for scatters and maps. */
  data_labels?: DataLabels
  legend?: LegendPosition
  /** Where a bar's value sits. */
  label_position?: LabelPosition
  /** Dots on a line's points. */
  markers?: Markers
  /** A stacked bar's total at the end of each stack. */
  stack_totals?: boolean
  /** Overrides for single series, keyed by the series' column. */
  series?: Record<string, SeriesFormat>
  /** Scatter and bubble: manual axis bounds. Absent means the standard's. */
  x_min?: number
  x_max?: number
  y_min?: number
  y_max?: number
  /** Scatter and bubble: a dashed line across each axis. */
  x_reference?: AxisReference
  y_reference?: AxisReference
  /** Scatter and bubble: the least-squares line through the points. */
  trend_line?: boolean
  /** Funnel: each stage's percentage of the top stage ("first", the default),
   *  of the stage above it ("previous"), or none. */
  funnel_percent?: FunnelPercent
}

export const UNIT_DIVISOR: Record<Unit, number> = { none: 1, K: 1e3, M: 1e6, B: 1e9 }

/* ---------------------------------------------------------------- scale */

export type Scale = { domain: [number, number]; ticks: number[] }

/** A step of 1, 2, 2.5 or 5 times a power of ten, at least `raw`. */
export function niceStep(raw: number): number {
  if (!Number.isFinite(raw) || raw <= 0) return 1
  const power = 10 ** Math.floor(Math.log10(raw))
  const fraction = raw / power
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10
  return nice * power
}

/** The next nice step below `step`: 10 -> 5 -> 2.5 -> 2 -> 1 -> 0.5. */
function niceStepBelow(step: number): number {
  const power = 10 ** Math.floor(Math.log10(step) + 1e-9)
  const fraction = Math.round((step / power) * 100) / 100
  const ladder = [1, 2, 2.5, 5, 10]
  const at = ladder.indexOf(fraction)
  return at > 0 ? ladder[at - 1] * power : power / 2
}

/** Digits needed to write a multiple of `step` exactly. */
function stepDecimals(step: number): number {
  if (step >= 1 && Number.isInteger(step)) return 0
  const text = step.toPrecision(6).replace(/0+$/, "")
  const dot = text.indexOf(".")
  return dot < 0 ? 0 : Math.min(6, text.length - dot - 1)
}

/**
 * A rounded range and evenly spaced round ticks that contain [lo, hi].
 *
 * `includeZero` is for marks measured FROM zero — bars, areas, stacks — where
 * a range without zero makes every length a lie. `pad` is for marks that are
 * positions — lines, scatter points — and adds a share of the data's span
 * first, so a point on the extreme does not sit on the plot's edge. Either
 * way the result is rounded outward to a nice step: rounding is what turns
 * 13.9M..30.6M into 10M..35M with a tick every 5M.
 */
export function niceScale(
  lo: number,
  hi: number,
  { includeZero = false, pad = 0, targetTicks = 5 }: {
    includeZero?: boolean
    pad?: number
    targetTicks?: number
  } = {}
): Scale | undefined {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return undefined
  if (lo > hi) [lo, hi] = [hi, lo]

  if (pad > 0 && hi > lo) {
    const extra = (hi - lo) * pad
    // Padding never pushes a range across zero it did not already cross.
    lo = lo >= 0 && lo - extra < 0 ? 0 : lo - extra
    hi = hi <= 0 && hi + extra > 0 ? 0 : hi + extra
  }
  if (includeZero) {
    lo = Math.min(lo, 0)
    hi = Math.max(hi, 0)
  }
  if (lo === hi) {
    const spread = lo === 0 ? 1 : Math.abs(lo) * 0.1
    lo -= spread
    hi += spread
    if (includeZero) {
      lo = Math.min(lo, 0)
      hi = Math.max(hi, 0)
    }
  }

  let step = niceStep((hi - lo) / Math.max(1, targetTicks - 1))
  let min = Math.floor(lo / step + 1e-9) * step
  let max = Math.ceil(hi / step - 1e-9) * step
  // A step chosen from the raw span can still give too many ticks once both
  // ends are rounded out; widen it until the count is readable.
  while ((max - min) / step > targetTicks + 1) {
    // The next nice step up: 1 -> 2 -> 2.5 -> 5 -> 10.
    step = niceStep(step * 1.01)
    min = Math.floor(lo / step + 1e-9) * step
    max = Math.ceil(hi / step - 1e-9) * step
  }

  /* A fitted axis must stay off zero. A step chosen for five ticks can round a
     range that starts well above zero straight back down to it: weekly lost
     sales of 36.6K..196K take a 50K step, floor to 0, and the "fitted" axis
     reads 0..250K — the zero-baseline chart the setting exists to avoid. Finer
     steps are tried, allowing a few more ticks, until the end holds off zero.
     A range that genuinely runs down near zero cannot hold off it within that
     allowance, and keeps zero. */
  if (!includeZero) {
    const collapsed = (low: number, high: number) =>
      (lo > 0 && low <= 0) || (hi < 0 && high >= 0)
    while (collapsed(min, max)) {
      const finer = niceStepBelow(step)
      const finerMin = Math.floor(lo / finer + 1e-9) * finer
      const finerMax = Math.ceil(hi / finer - 1e-9) * finer
      if ((finerMax - finerMin) / finer > targetTicks + 3) break
      step = finer
      min = finerMin
      max = finerMax
    }
  }

  const decimals = stepDecimals(step)
  const count = Math.round((max - min) / step)
  const ticks = Array.from({ length: count + 1 }, (_, i) =>
    Number((min + i * step).toFixed(decimals))
  )
  return { domain: [ticks[0], ticks[ticks.length - 1]], ticks }
}

/** The extent of `keys` across `rows`, ignoring anything non-numeric. */
export function extent(
  rows: ReadonlyArray<Record<string, unknown>>,
  keys: readonly string[]
): [number, number] | undefined {
  let lo = Infinity
  let hi = -Infinity
  for (const row of rows) {
    for (const key of keys) {
      const value = row[key]
      if (typeof value !== "number" || !Number.isFinite(value)) continue
      if (value < lo) lo = value
      if (value > hi) hi = value
    }
  }
  return Number.isFinite(lo) ? [lo, hi] : undefined
}

/** Per-row totals of stacked series — the height of each stack. */
export function stackTotals(
  rows: ReadonlyArray<Record<string, unknown>>,
  keys: readonly string[]
): [number, number] | undefined {
  let lo = 0
  let hi = 0
  let seen = false
  for (const row of rows) {
    let positive = 0
    let negative = 0
    for (const key of keys) {
      const value = row[key]
      if (typeof value !== "number" || !Number.isFinite(value)) continue
      seen = true
      if (value >= 0) positive += value
      else negative += value
    }
    hi = Math.max(hi, positive)
    lo = Math.min(lo, negative)
  }
  return seen ? [lo, hi] : undefined
}

/**
 * The scale for a stack of percentages that are shares of a whole.
 *
 * Each share is rounded on its own, so a stack of 11.5 + 57.2 + 25.3 + 6.2
 * totals 100.2 and the axis used to read "100.2%". The range still reaches the
 * tallest stack, so nothing is clipped, but the ticks stop at a clean 100.
 * Undefined when the stacks are not shares of one whole.
 */
export function shareStackScale(totals: [number, number] | undefined): Scale | undefined {
  if (!totals) return undefined
  const [lo, hi] = totals
  if (lo < 0 || hi < 99 || hi > 101) return undefined
  return { domain: [0, Math.max(100, hi)], ticks: [0, 25, 50, 75, 100] }
}

/* ----------------------------------------------------------------- units */

export type AxisKind = "currency" | "percent" | "ratio" | "number"

/** How a value column's numbers read: declared format first, then its name. */
export function valueKind(name: string | null | undefined, formats?: ColumnFormats | null): AxisKind {
  const declared = name ? formats?.[name]?.format : undefined
  if (declared === "currency" || declared === "percent" || declared === "ratio") return declared
  if (declared) return "number"
  const guessed = classify(name)
  return guessed === "plain" ? "number" : guessed
}

/** The largest unit a number of this size reads naturally in. */
export function naturalUnit(maxAbs: number): Unit {
  if (maxAbs >= 1e9) return "B"
  if (maxAbs >= 1e6) return "M"
  if (maxAbs >= 1e3) return "K"
  return "none"
}

/**
 * One currency unit for a whole dashboard, or undefined to leave each chart
 * its own.
 *
 * Mixing millions on one chart and billions on the next makes a reader stop
 * and convert before comparing. The largest natural unit among the
 * dashboard's currency charts is the shared one — but a chart whose numbers
 * would read below 0.1 of it keeps its own unit (see usesSharedUnit), because
 * an average order value of AED 245 written as "0.0M" says nothing at all.
 */
export function sharedCurrencyUnit(maxima: readonly number[]): Unit | undefined {
  const units = maxima
    .filter((m) => Number.isFinite(m) && m > 0)
    .map((m) => naturalUnit(m))
  if (units.length < 2) return undefined
  const order: Unit[] = ["none", "K", "M", "B"]
  const largest = units.reduce((a, b) => (order.indexOf(b) > order.indexOf(a) ? b : a))
  return largest === "none" ? undefined : largest
}

/** Whether a chart of this size should adopt the dashboard's shared unit. */
export function usesSharedUnit(maxAbs: number, shared: Unit | undefined): boolean {
  return Boolean(shared) && maxAbs >= UNIT_DIVISOR[shared as Unit] * 0.1
}

export type AxisFormat = {
  kind: AxisKind
  unit: Unit
  decimals: number
  /** The currency code, when kind is currency. Undefined means US dollars. */
  currency?: string
}

/**
 * How one value axis writes its numbers, decided ONCE for all of its ticks.
 *
 * Formatting each tick on its own is what let one axis read "900K" and
 * "1.2M": every value chose its own abbreviation. Here the unit comes from the
 * axis as a whole, and the decimals from its step, so every tick agrees.
 */
export function axisFormat({
  name,
  formats,
  defaultCurrency,
  scale,
  unit,
  sharedUnit,
}: {
  name: string | null | undefined
  formats?: ColumnFormats | null
  defaultCurrency?: string | null
  scale: Scale | undefined
  /** The panel's own setting, which wins over everything. */
  unit?: Unit
  /** The dashboard's shared currency unit. */
  sharedUnit?: Unit
}): AxisFormat {
  const kind = valueKind(name, formats)
  const declaredCurrency = name ? formats?.[name]?.currency : undefined
  const currency =
    kind === "currency"
      ? declaredCurrency !== undefined ? declaredCurrency : defaultCurrency ?? undefined
      : undefined

  const ticks = scale?.ticks ?? []
  const maxAbs = ticks.reduce((m, t) => Math.max(m, Math.abs(t)), 0)
  const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : maxAbs || 1

  let chosen: Unit = "none"
  if (kind === "currency" || kind === "number") {
    if (unit) chosen = unit
    else if (kind === "currency" && usesSharedUnit(maxAbs, sharedUnit)) chosen = sharedUnit as Unit
    else chosen = naturalUnit(maxAbs)
  }
  const scaled = step / UNIT_DIVISOR[chosen]
  const decimals = kind === "percent" || kind === "ratio" || chosen !== "none"
    ? Math.min(2, stepDecimals(Number(scaled.toPrecision(6))))
    : Math.min(2, stepDecimals(step))
  return { kind, unit: chosen, decimals, currency }
}

/**
 * One tick, written in the axis's shared unit.
 *
 * The currency code goes on the tick only when no title carries it: "AED" on
 * all six ticks of an axis titled "Net Sales (AED)" is width spent saying the
 * same thing six times.
 */
export function formatTick(value: number, fmt: AxisFormat, { currencyInTitle = false } = {}): string {
  if (!Number.isFinite(value)) return ""
  const sign = value < 0 ? "-" : ""
  const abs = Math.abs(value)
  const body = (abs / UNIT_DIVISOR[fmt.unit]).toLocaleString("en-US", {
    minimumFractionDigits: fmt.decimals,
    maximumFractionDigits: fmt.decimals,
  })
  const letter = fmt.unit === "none" ? "" : fmt.unit
  switch (fmt.kind) {
    case "percent":
      return `${sign}${body}%`
    case "ratio":
      return `${sign}${body}x`
    case "currency": {
      if (currencyInTitle) return `${sign}${body}${letter}`
      const { prefix, suffix } = currencyAffix(fmt.currency)
      return `${sign}${prefix}${body}${letter}${suffix}`
    }
    default:
      return `${sign}${body}${letter}`
  }
}

/**
 * A value in a data label: compact, in its own natural unit unless the panel
 * chose one, in which case every label uses it.
 */
export function formatLabelValue(
  value: number,
  fmt: AxisFormat,
  {
    unit: setting,
    currencyInTitle = false,
    fractional = false,
  }: {
    unit?: Unit
    currencyInTitle?: boolean
    /** Other values in the series have decimals, so a whole one shows one too. */
    fractional?: boolean
  } = {}
): string {
  const scalable = fmt.kind === "currency" || fmt.kind === "number"
  const unit = scalable ? setting ?? naturalUnit(Math.abs(value)) : "none"
  const scaled = Math.abs(value) / UNIT_DIVISOR[unit]
  const decimals =
    unit === "none" && Number.isInteger(value) && !fractional ? 0
    : scaled >= 100 ? 0
    : scaled >= 1 || unit === "none" ? 1
    : 2
  return formatTick(value, { ...fmt, unit, decimals }, { currencyInTitle })
}

/* --------------------------------------------------------------- scatter */

/**
 * An axis with bounds a person set, in place of the standard's.
 *
 * Only the ends that were set move; the other keeps the standard's rounded
 * end. Ticks fall on round numbers inside the range, so a bound of 10.8 does
 * not print as a tick of its own.
 */
export function boundedScale(
  standard: Scale | undefined,
  { min, max }: { min?: number; max?: number }
): Scale | undefined {
  if (min === undefined && max === undefined) return standard
  const lo = min ?? standard?.domain[0]
  const hi = max ?? standard?.domain[1]
  if (lo === undefined || hi === undefined || !(hi > lo)) return standard
  const step = niceStep((hi - lo) / 4)
  const decimals = stepDecimals(step)
  const ticks: number[] = []
  for (let t = Math.ceil(lo / step - 1e-9) * step; t <= hi + step * 1e-9; t += step) {
    ticks.push(Number(t.toFixed(decimals)))
  }
  return { domain: [lo, hi], ticks }
}

/** The least-squares line through points, or undefined when x does not vary. */
export function linearFit(
  points: ReadonlyArray<readonly [number, number]>
): { slope: number; intercept: number } | undefined {
  const n = points.length
  if (n < 2) return undefined
  let sx = 0, sy = 0, sxx = 0, sxy = 0
  for (const [x, y] of points) {
    sx += x
    sy += y
    sxx += x * x
    sxy += x * y
  }
  const denominator = n * sxx - sx * sx
  if (Math.abs(denominator) < 1e-12) return undefined
  const slope = (n * sxy - sx * sy) / denominator
  return { slope, intercept: (sy - slope * sx) / n }
}

/** The part of a line y = slope·x + intercept that lies inside both ranges. */
export function clipLine(
  fit: { slope: number; intercept: number },
  [x0, x1]: readonly [number, number],
  [y0, y1]: readonly [number, number]
): [{ x: number; y: number }, { x: number; y: number }] | undefined {
  const at = (x: number) => fit.slope * x + fit.intercept
  let a = x0
  let b = x1
  if (fit.slope !== 0) {
    // Where the line meets the bottom and top of the plot.
    const xLow = (y0 - fit.intercept) / fit.slope
    const xHigh = (y1 - fit.intercept) / fit.slope
    a = Math.max(a, Math.min(xLow, xHigh))
    b = Math.min(b, Math.max(xLow, xHigh))
  } else if (at(x0) < y0 || at(x0) > y1) {
    return undefined
  }
  if (!(b > a)) return undefined
  return [{ x: a, y: at(a) }, { x: b, y: at(b) }]
}

/** Where a reference line sits, from the axis' values. */
export function referenceValue(
  values: readonly number[],
  reference: AxisReference | undefined
): number | undefined {
  if (!reference) return undefined
  if (reference.kind === "value") {
    return typeof reference.value === "number" && Number.isFinite(reference.value)
      ? reference.value
      : undefined
  }
  if (values.length === 0) return undefined
  if (reference.kind === "mean") return values.reduce((sum, v) => sum + v, 0) / values.length
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

/** The smallest radius a bubble is drawn at, so a tiny value stays visible. */
export const BUBBLE_MIN_R = 2.5

/**
 * A bubble's radius, with its AREA in proportion to the value.
 *
 * Scaling the radius instead squares every difference: a value twice another
 * drew a bubble four times the size.
 */
export function bubbleRadius(value: unknown, largest: number, maxRadius: number): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0 || largest <= 0) {
    return BUBBLE_MIN_R
  }
  return Math.max(BUBBLE_MIN_R, maxRadius * Math.sqrt(Math.min(value, largest) / largest))
}

/** Round values for a size legend: the largest nice value, its half and tenth. */
export function sizeLegendValues(largest: number): number[] {
  if (!(largest > 0)) return []
  const power = 10 ** Math.floor(Math.log10(largest))
  const top = [5, 2.5, 2, 1].map((m) => m * power).find((v) => v <= largest) ?? power
  return [top / 10, top / 2, top]
}

/* ---------------------------------------------------------------- titles */

/** What the currency is called in a title: its code, or USD when unnamed. */
function currencyName(code: string | undefined): string {
  if (code === undefined) return "USD"
  return code.trim().toUpperCase()
}

/**
 * The value axis title the standard gives a chart.
 *
 * One series: its readable name, with the currency after it — "Net Sales
 * (AED)". Several: the legend names them, so the title only states what they
 * share, and only when that is a currency. Undefined means no title.
 */
export function valueAxisTitle(
  keys: readonly string[],
  fmt: AxisFormat,
  formats?: ColumnFormats | null,
  columns?: readonly string[] | null
): string | undefined {
  const code = fmt.kind === "currency" ? currencyName(fmt.currency) : ""
  if (keys.length === 1) {
    const label = seriesLabel(keys[0], formats, columns ?? null)
    return code ? `${label} (${code})` : label
  }
  return code || undefined
}

/** Whether a title will print the currency, so ticks can leave it off. */
export function titleCarriesCurrency(fmt: AxisFormat, title: string | undefined): boolean {
  return fmt.kind === "currency" && Boolean(title) && fmt.currency !== ""
}

const DAY_MS = 86_400_000

/**
 * The title for a date axis, named after its period rather than its column:
 * "Week", not "Week Start". Undefined when the values are not dates.
 */
export function dateAxisTitle(values: readonly unknown[]): string | undefined {
  const times: number[] = []
  for (const value of values) {
    const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value ?? ""))
    if (!match) return undefined
    times.push(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])))
  }
  if (times.length < 2) return times.length ? "Date" : undefined
  const gaps = times.slice(1).map((t, i) => Math.round((t - times[i]) / DAY_MS))
  const typical = gaps.sort((a, b) => a - b)[Math.floor(gaps.length / 2)]
  if (typical >= 360) return "Year"
  if (typical >= 88) return "Quarter"
  if (typical >= 28) return "Month"
  if (typical === 7) return "Week"
  return "Date"
}

/** Whether a run of ISO dates crosses a calendar year. */
export function spansYears(values: readonly unknown[]): boolean {
  const years = new Set<string>()
  for (const value of values) {
    const match = /^(\d{4})-/.exec(String(value ?? ""))
    if (match) years.add(match[1])
    if (years.size > 1) return true
  }
  return false
}

/** A setting's value when set, else the standard's. */
export function titleOr(setting: string | undefined, standard: string | undefined): string | undefined {
  if (setting === undefined) return standard
  return setting.trim() === "" ? undefined : setting.trim()
}

/* ---------------------------------------------------------------- labels */

/** Indices of the largest and smallest numeric value, or undefined. */
export function extremes(values: readonly unknown[]): { max: number; min: number } | undefined {
  let max = -1
  let min = -1
  values.forEach((value, i) => {
    if (typeof value !== "number" || !Number.isFinite(value)) return
    if (max < 0 || value > (values[max] as number)) max = i
    if (min < 0 || value < (values[min] as number)) min = i
  })
  return max < 0 ? undefined : { max, min }
}

/** Width of one label character at 11px, and a label's line height, in px. */
const LABEL_CHAR_PX = 6.2
const LABEL_LINE_PX = 13

/**
 * Where each point label goes so labels do not print over one another.
 *
 * Positions are pixels within the plot. Each label with text, taken top to
 * bottom, keeps its preferred place if it covers no point and no label already
 * placed. Otherwise it tries the other side of its point, then one line down
 * or up, then two, then three — on each side — and takes the first spot that
 * is clear. A label with no clear spot stays where it was preferred: a slight
 * overlap reads better than a name far from its point.
 *
 * With a plot width given, a spot that runs off either edge does not count as
 * clear. Without that, resolving a collision by flipping a label to the other
 * side pushed it past the edge of the plot, where the chart clips it: a point
 * near the left edge lost its name to "…obile App" — the collision was solved
 * and the label was gone. A label that fits nowhere inside takes the side with
 * more room, since a crowded name still reads and a clipped one does not.
 */
export function spreadLabels(
  labels: ReadonlyArray<{ text: string; px: number; py: number; left: boolean }>,
  /** Width of the plot in px, measured the same way as each label's `px`. */
  plotWidth?: number
): Array<{ dy: number; left: boolean }> {
  const out = labels.map((label) => ({ dy: 0, left: label.left }))
  // Every point is an obstacle too, not only the labels: a label's halo drawn
  // over a neighbouring point hides that point entirely. A label never
  // reaches its own point, which sits 9px away from its text.
  const placed: Array<{ x0: number; x1: number; y0: number; y1: number }> = labels.map(
    (label) => ({ x0: label.px - 5, x1: label.px + 5, y0: label.py - 5, y1: label.py + 5 })
  )
  const order = labels
    .map((label, i) => ({ label, i }))
    .filter(({ label }) => label.text)
    .sort((a, b) => a.label.py - b.label.py)

  for (const { label, i } of order) {
    const width = label.text.length * LABEL_CHAR_PX
    const box = (left: boolean, dy: number) => {
      const x0 = left ? label.px - 9 - width : label.px + 9
      return {
        x0,
        x1: x0 + width,
        y0: label.py + dy - LABEL_LINE_PX / 2,
        y1: label.py + dy + LABEL_LINE_PX / 2,
      }
    }
    const inside = (b: ReturnType<typeof box>) =>
      plotWidth === undefined || (b.x0 >= 0 && b.x1 <= plotWidth)
    const clear = (b: ReturnType<typeof box>) =>
      inside(b) &&
      placed.every((p) => b.x1 <= p.x0 || b.x0 >= p.x1 || b.y1 <= p.y0 || b.y0 >= p.y1)

    const lines = [0, 1, -1, 2, -2, 3, -3]
    const sides = [label.left, !label.left]
    let chosen: { dy: number; left: boolean } | undefined
    for (const n of lines) {
      for (const left of sides) {
        if (clear(box(left, n * LABEL_LINE_PX))) {
          chosen = { dy: n * LABEL_LINE_PX, left }
          break
        }
      }
      if (chosen) break
    }
    const roomier =
      plotWidth === undefined
        ? label.left
        : label.px > plotWidth - label.px
    out[i] = chosen ?? { dy: 0, left: roomier }
    placed.push(box(out[i].left, out[i].dy))
  }
  return out
}

/** Beyond this many points a series draws no per-point markers. */
export const MARKER_LIMIT = 24

/** A line's point marker: filled with the background, ringed in the series colour. */
export const MARKER_R = 3.5
export const MARKER_STROKE = 2

export function markerDot(color: string) {
  return { r: MARKER_R, fill: "var(--background)", stroke: color, strokeWidth: MARKER_STROKE }
}

/** A scatter labels every point up to this many; past it, the extremes. */
export const SCATTER_LABEL_LIMIT = 15

/** The indices a label setting picks from one series' values. */
export function labelIndices(values: readonly unknown[], mode: DataLabels): number[] {
  if (mode === "none") return []
  if (mode === "all") {
    return values.flatMap((v, i) => (typeof v === "number" && Number.isFinite(v) ? [i] : []))
  }
  const found = extremes(values)
  if (!found) return []
  if (mode === "max") return [found.max]
  if (mode === "min") return [found.min]
  return found.max === found.min ? [found.max] : [found.max, found.min]
}

export type LabelBox = {
  x0: number
  x1: number
  y0: number
  y1: number
  /** The series an obstacle belongs to, whose own labels it does not block. */
  owner?: number
  /** A line: kept clear where possible, crossed by a highest or lowest label if not. */
  soft?: boolean
  /** A bar, which blocks other bars' labels but not a line's. */
  bar?: boolean
}

/** One spot a label may take, relative to its anchor. */
export type LabelSpot = {
  dx: number
  dy: number
  anchor: "start" | "middle" | "end"
  /** Drawn over the shape it belongs to, in a colour readable on it. */
  inside?: boolean
  /** Turned to read upwards, for a bar narrower than its label. */
  rotate?: boolean
}

export type LabelCandidate = {
  text: string
  x: number
  y: number
  /** Spots in order of preference; the first clear one wins. */
  spots: LabelSpot[]
  /** Lower goes first, so it keeps its preferred spot. */
  priority: number
  /** For an inside spot: the shape the text must fit within. */
  within?: LabelBox
  /** Text size in px; 11 when absent. */
  fontSize?: number
  /** The series the label belongs to. */
  owner?: number
  /** A point's label, which may sit over a bar: its halo keeps it readable. */
  overBars?: boolean
}

export type PlacedLabel = {
  index: number
  x: number
  y: number
  anchor: LabelSpot["anchor"]
  inside: boolean
  rotate: boolean
}

/** Glyph widths of the label face at 11px, semibold, measured in the page. */
function glyphWidth(ch: string): number {
  if (ch >= "0" && ch <= "9") return 7.1
  if (ch === "." || ch === "," || ch === " ") return 3
  if (ch === "%") return 11.1
  if (ch === "M" || ch === "W") return 10.1
  if (ch >= "A" && ch <= "Z") return 7.6
  if (ch >= "a" && ch <= "z") return 5.9
  return 7.1
}

/** Width of a value label's text, in px. */
export function labelWidth(text: string, fontSize = 11): number {
  let width = 0
  for (const ch of text) width += glyphWidth(ch)
  return (width * fontSize) / 11
}

function labelBox(c: LabelCandidate, spot: LabelSpot): LabelBox {
  const size = c.fontSize ?? 11
  const length = labelWidth(c.text, size)
  const thickness = (LABEL_LINE_PX * size) / 11
  const x = c.x + spot.dx
  const y = c.y + spot.dy
  if (spot.rotate) {
    // Read upwards: the text runs from its anchor towards the top.
    const y0 = spot.anchor === "start" ? y - length : spot.anchor === "end" ? y : y - length / 2
    return { x0: x - thickness / 2, x1: x + thickness / 2, y0, y1: y0 + length }
  }
  const x0 = spot.anchor === "start" ? x : spot.anchor === "end" ? x - length : x - length / 2
  return { x0, x1: x0 + length, y0: y - thickness / 2, y1: y + thickness / 2 }
}

/** How far inside text may run past the edge of its shape, in px. */
const INSIDE_SLACK = 3

const GRID_PX = 48

/** Boxes bucketed by area, so a check looks only at its neighbours. */
class BoxGrid {
  private cells = new Map<string, LabelBox[]>()

  private keys(b: LabelBox): string[] {
    const out: string[] = []
    for (let gx = Math.floor(b.x0 / GRID_PX); gx <= Math.floor(b.x1 / GRID_PX); gx++) {
      for (let gy = Math.floor(b.y0 / GRID_PX); gy <= Math.floor(b.y1 / GRID_PX); gy++) {
        out.push(`${gx}:${gy}`)
      }
    }
    return out
  }

  add(b: LabelBox) {
    for (const key of this.keys(b)) {
      const cell = this.cells.get(key)
      if (cell) cell.push(b)
      else this.cells.set(key, [b])
    }
  }

  clear(b: LabelBox, owner?: number, ignoreSoft = false, overBars = false): boolean {
    for (const key of this.keys(b)) {
      for (const p of this.cells.get(key) ?? []) {
        if (owner !== undefined && p.owner === owner) continue
        if (ignoreSoft && p.soft) continue
        if (overBars && p.bar) continue
        if (b.x1 > p.x0 && b.x0 < p.x1 && b.y1 > p.y0 && b.y0 < p.y1) return false
      }
    }
    return true
  }
}

/**
 * Where each value label goes, so none prints over another.
 *
 * Candidates are taken by priority. Each takes the first of its spots that
 * lies inside the bounds, covers no obstacle (a point or a bar) and no label
 * already placed. A candidate with no clear spot is left out: on a crowded
 * chart a missing label reads better than two unreadable ones.
 */
export function placeLabels(
  candidates: readonly LabelCandidate[],
  bounds: LabelBox,
  obstacles: readonly LabelBox[] = []
): PlacedLabel[] {
  const labels = new BoxGrid()
  const blocked = new BoxGrid()
  for (const o of obstacles) blocked.add(o)

  const order = candidates
    .map((c, index) => ({ c, index }))
    .filter(({ c }) => c.text)
    .sort((a, b) => a.c.priority - b.c.priority || a.index - b.index)

  const placed: PlacedLabel[] = []
  const tryPlace = (c: LabelCandidate, index: number, ignoreSoft: boolean): boolean => {
    for (const spot of c.spots) {
      const box = labelBox(c, spot)
      const inBounds =
        box.x0 >= bounds.x0 && box.x1 <= bounds.x1 && box.y0 >= bounds.y0 && box.y1 <= bounds.y1
      if (!inBounds || !labels.clear(box)) continue
      if (spot.inside) {
        const w = c.within
        const fits =
          w &&
          box.x0 >= w.x0 - INSIDE_SLACK && box.x1 <= w.x1 + INSIDE_SLACK &&
          box.y0 >= w.y0 - INSIDE_SLACK && box.y1 <= w.y1 + INSIDE_SLACK
        if (!fits) continue
      }
      // Inside or out, a label keeps off other series' lines, points and bars.
      if (!blocked.clear(box, c.owner, ignoreSoft, c.overBars)) continue
      labels.add(box)
      placed.push({
        index,
        x: c.x + spot.dx,
        y: c.y + spot.dy,
        anchor: spot.anchor,
        inside: Boolean(spot.inside),
        rotate: Boolean(spot.rotate),
      })
      return true
    }
    return false
  }

  const missed: Array<{ c: LabelCandidate; index: number }> = []
  for (const { c, index } of order) {
    if (!tryPlace(c, index, false) && (c.priority <= 0 || c.within)) missed.push({ c, index })
  }
  // A highest or lowest value, or a bar's value, blocked only by another
  // series' line still goes on, over the line: a bar left without its value
  // or a chart without its peak costs more than the crossing, which the
  // label's halo keeps readable.
  for (const { c, index } of missed) tryPlace(c, index, true)
  return placed
}

/** Room above the plot a label over its point needs. */
export const LABEL_ROOM = 20

/** Room above the plot an upright label over a narrow bar needs. */
export const UPRIGHT_LABEL_ROOM = 40

/** Right-hand room a horizontal bar's labels need beyond the plot. */
export function horizontalLabelRoom(
  data: ReadonlyArray<Record<string, unknown>>,
  keys: readonly string[],
  fmt: AxisFormat,
  unit: Unit | undefined,
  currencyInTitle = false
): number {
  let widest = 0
  for (const row of data) {
    for (const key of keys) {
      const value = row[key]
      if (typeof value === "number" && Number.isFinite(value)) {
        widest = Math.max(widest, labelWidth(formatLabelValue(value, fmt, { unit, currencyInTitle })))
      }
    }
  }
  return Math.min(64, Math.ceil(widest) + 8)
}

/** Dark or light text, whichever reads on a fill colour. */
export function textOn(fill: string): "#ffffff" | "#1f2937" {
  const hex = /^#([0-9a-f]{6})$/i.exec(fill)
  let r: number, g: number, b: number
  if (hex) {
    const n = parseInt(hex[1], 16)
    r = (n >> 16) & 255
    g = (n >> 8) & 255
    b = n & 255
  } else {
    const hsl = /^hsl\((\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%\)$/.exec(fill)
    if (!hsl) return "#ffffff"
    return Number(hsl[3]) > 60 ? "#1f2937" : "#ffffff"
  }
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
  return luminance > 0.62 ? "#1f2937" : "#ffffff"
}

/** Which data labels a chart shows: the panel's setting, else the standard. */
export function dataLabelsFor(
  chartType: string | null | undefined,
  pointCount: number,
  setting: DataLabels | undefined
): DataLabels {
  if (setting) return setting
  switch (chartType) {
    case "geo_countries":
    case "geo_us_states":
      return "extremes"
    case "scatter":
    case "bubble":
      return pointCount <= SCATTER_LABEL_LIMIT ? "all" : "extremes"
    case "line":
    case "area":
    case "area_line":
      return "max"
    default:
      return "none"
  }
}

/**
 * The label setting for one series: its own override, else the chart's.
 *
 * A reference series (a target line) is left unlabelled unless it is chosen
 * by name: its value is known before the chart is drawn, and labelling it
 * doubled every label on a chart of actual against budget.
 */
export function seriesLabels(
  chartType: string | null | undefined,
  pointCount: number,
  format: PanelFormat | null | undefined,
  key: string | undefined,
  { reference = false, overridable = true }: { reference?: boolean; overridable?: boolean } = {}
): DataLabels {
  const own = overridable && key ? format?.series?.[key]?.data_labels : undefined
  if (own) return own
  if (reference) return "none"
  return dataLabelsFor(chartType, pointCount, format?.data_labels)
}

/** How a line's points are marked: every point, only labelled ones, or none. */
export type MarkerMode = "all" | "labelled" | "none"

export function seriesMarkers(
  format: PanelFormat | null | undefined,
  key: string | undefined,
  pointCount: number,
  { reference = false, overridable = true }: { reference?: boolean; overridable?: boolean } = {}
): MarkerMode {
  const own = overridable && key ? format?.series?.[key]?.markers : undefined
  const setting = own ?? (reference ? "hide" : format?.markers ?? "auto")
  if (setting === "show") return "all"
  if (setting === "hide") return "none"
  return pointCount <= MARKER_LIMIT ? "all" : "labelled"
}

export function seriesLabelPosition(
  format: PanelFormat | null | undefined,
  key: string | undefined,
  { overridable = true }: { overridable?: boolean } = {}
): LabelPosition {
  const own = overridable && key ? format?.series?.[key]?.label_position : undefined
  return own ?? format?.label_position ?? "auto"
}

/** How one series of a chart is drawn, which decides the settings it offers. */
export type SeriesShape = {
  key: string
  kind: "bar" | "line" | "segment"
  /** A target or budget line drawn for comparison. */
  reference?: boolean
}

/**
 * The series a panel draws from its value columns, for settings by series.
 *
 * Empty when the series come from the values of a split column instead: those
 * change from one run to the next, so a setting kept against one could stop
 * matching without anyone noticing.
 */
export function panelSeriesShapes(
  chartType: string | null | undefined,
  y: string | readonly string[] | null | undefined,
  color: string | null | undefined
): SeriesShape[] {
  const keys = Array.isArray(y) ? [...y] : y ? [y as string] : []
  if (color || keys.length < 2) return []
  const last = keys.length - 1
  switch (chartType) {
    case "bar":
    case "grouped_bar":
      return keys.map((key) => ({ key, kind: "bar" }))
    case "stacked_bar":
      return keys.map((key) => ({ key, kind: "segment" }))
    case "line":
    case "area":
      return keys.map((key) => ({ key, kind: "line" }))
    case "combo":
      return keys.map((key, i) => ({ key, kind: i === last ? "line" : "bar" }))
    case "area_line":
      return keys.map((key, i) => ({ key, kind: "line", reference: i === last }))
    default:
      return []
  }
}

/** Where the legend goes: the panel's setting, else bottom when it is needed. */
export function legendFor(
  seriesCount: number,
  setting: LegendPosition | undefined,
  { labelledDirectly = false }: { labelledDirectly?: boolean } = {}
): LegendPosition {
  if (setting) return setting
  if (seriesCount < 2 || labelledDirectly) return "none"
  return "bottom"
}

/* ------------------------------------------------------------ funnel */

/** A stage name longer than this is shortened where it is repeated in a note. */
const FUNNEL_NAME_CHARS = 18

function funnelPercent(ratio: number): string {
  const pct = ratio * 100
  // Two places under 1%, where one would round a real 0.4% to 0% or 1%.
  const text = pct < 1 ? pct.toFixed(2) : pct.toFixed(1)
  return `${text.replace(/\.?0+$/, "")}%`
}

/**
 * A funnel's stages, largest first.
 *
 * A funnel only reads as one when every stage is narrower than the stage above
 * it, so its percentages fall from 100% and the last one says how much of the
 * top was converted. Data does not always arrive in that order: product views
 * outnumber the sessions they happen in. Stages of equal size keep the order
 * the query gave them.
 */
export function sortFunnelStages<T extends { value: number }>(stages: readonly T[]): T[] {
  return stages
    .map((stage, index) => ({ stage, index }))
    .sort((a, b) => b.stage.value - a.stage.value || a.index - b.index)
    .map(({ stage }) => stage)
}

/**
 * The percentage under each funnel stage's label, for stages already sorted
 * largest first.
 *
 *   first     "100%", then "8.5% of Sessions" — the share of the top stage,
 *             so the last stage is the funnel's overall conversion. Default.
 *   previous  "100%", then "55% of previous" — each step's conversion.
 *   none      no percentages.
 *
 * Empty strings when the top stage is not a positive number, since a share of
 * it means nothing.
 */
export function funnelStageNotes(
  values: readonly number[],
  names: readonly string[],
  mode: FunnelPercent = "first"
): string[] {
  const top = values[0]
  if (mode === "none" || !(typeof top === "number" && Number.isFinite(top) && top > 0)) {
    return values.map(() => "")
  }
  const topName = (names[0] ?? "").trim()
  const shortTop = topName.length > FUNNEL_NAME_CHARS
    ? `${topName.slice(0, FUNNEL_NAME_CHARS - 1).trimEnd()}…`
    : topName
  return values.map((value, i) => {
    if (i === 0) return "100%"
    const base = mode === "previous" ? values[i - 1] : top
    if (!(typeof base === "number" && base > 0)) return ""
    // Sorted stages never exceed their base; min() keeps a caller that did not
    // sort from printing a conversion above 100%.
    const ratio = Math.min(value / base, 1)
    return `${funnelPercent(ratio)} of ${mode === "previous" ? "previous" : shortTop || "top stage"}`
  })
}
