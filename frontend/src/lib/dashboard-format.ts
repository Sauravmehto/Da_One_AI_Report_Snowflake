/**
 * Value formatting for dashboard panels.
 *
 * Ported from the MCP dashboard renderer — this is the part worth keeping.
 * The rules were arrived at by fixing real defects, so the reasoning is kept
 * with them:
 *
 * - Column NAMES decide the unit (currency / percent / ratio), because panel
 *   SQL aliases carry that meaning and nothing else does.
 * - Negative money is `-$5M`, never `$-5M` — sign before the symbol.
 * - Percent columns arrive already scaled to whole numbers; the semantic layer
 *   is authoritative and a `_PCT` suffix alone means nothing.
 */

const CURRENCY =
  /(PRINCIPAL|BALANCE|VALUE|EXPENSE|NAV|AMOUNT|PRICE|REVENUE|EBITDA|PROCEEDS|CASH|FEE|GAIN|LOSS|G\/L|CAPITAL|COST|EXPOSURE|FAIR_?VALUE|PROFIT|DEBT|EQUITY)/
const PERCENT = /(PCT|PERCENT|MARGIN|YIELD|_RATE\b|^RATE|RATE_|DELINQ|\bIRR\b)/
const RATIO = /(DSCR|LTV|TVPI|DPI|RVPI|MOIC|RATIO|LEVERAGE|COVERAGE)/
/* The same codes CURRENCY_SUFFIXES lists, as the last part of a column name. */
const CURRENCY_CODE_SUFFIX = /_(AED|BHD|EUR|GBP|INR|KWD|OMR|QAR|SAR|USD)$/

export type ValueKind = "currency" | "percent" | "ratio" | "plain"

export function classify(name: string | null | undefined): ValueKind {
  const u = String(name ?? "")
    .toUpperCase()
    .trim()
  if (u.endsWith("%")) return "percent" // an explicit % in the label always wins
  // A column that ends in a currency code is money whatever comes before it.
  // NET_SALES_AED matched none of the words below and read as a bare count,
  // and GROSS_MARGIN_AED matched MARGIN and read as a percent.
  if (CURRENCY_CODE_SUFFIX.test(u)) return "currency"
  if (PERCENT.test(u)) return "percent"
  if (RATIO.test(u)) return "ratio"
  if (CURRENCY.test(u)) return "currency"
  return "plain"
}

const NF = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 })
const NF0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 })

/**
 * Decimals enough to keep a small percentage distinguishable.
 *
 * A flat `toFixed(1)` collapsed every value below 1 into "0.1%" or "0.0%" — a
 * column of rates rendered as a column of identical noise. Scale up the
 * precision as the magnitude falls so the digits that carry the information
 * survive, and cap it so ordinary percentages stay clean.
 */
function percentDecimals(value: number): number {
  const a = Math.abs(value)
  if (a === 0) return 0
  if (a >= 10) return 1
  if (a >= 1) return 2
  if (a >= 0.1) return 3
  return 4
}

function percent(value: number): string {
  const fixed = value.toFixed(percentDecimals(value))
  // Trailing zeros are trimmed only inside the fractional part — stripping
  // them unconditionally would turn "100" into "1".
  const trimmed = fixed.includes(".")
    ? fixed.replace(/0+$/, "").replace(/\.$/, "")
    : fixed
  return `${trimmed}%`
}

/**
 * Compact magnitude, one decimal. Rounds from a representable scale (/1e5)
 * so 2,150,000 gives 2.2M rather than 2.1M — dividing by 1e6 first would
 * store 2.1499… and round the wrong way.
 */
export function abbreviate(value: number): string {
  const a = Math.abs(value)
  if (a >= 1e9) return `${(Math.round(value / 1e8) / 10).toFixed(1)}B`
  if (a >= 1e6) return `${(Math.round(value / 1e5) / 10).toFixed(1)}M`
  if (a >= 1e3) return `${(Math.round(value / 1e2) / 10).toFixed(1)}K`
  return NF.format(value)
}

/** What goes before and after the digits for one currency. */
type CurrencyAffix = { prefix: string; suffix: string }

const NO_AFFIX: CurrencyAffix = { prefix: "", suffix: "" }
const USD_AFFIX: CurrencyAffix = { prefix: "$", suffix: "" }

/* Derived once per code. Intl is not cheap to construct and a table redrawn by
   hand rots — it would have to know the symbol, whether it leads or trails, and
   whether a space belongs between, and all three vary by currency. */
const AFFIX_CACHE = new Map<string, CurrencyAffix>()

/**
 * Where the symbol sits, taken from `Intl` rather than a hand-written table.
 *
 * Three states, and the last two are NOT the same thing:
 *   undefined  the author said nothing — US dollars, so every dashboard
 *              written before this renders exactly as it did
 *   "AED"      a named currency
 *   ""         the author is saying they do not know, or the column mixes
 *              currencies. No affix at all.
 *
 * A code Intl rejects yields no affix either. A bare number is unhelpful; a
 * wrong symbol is a lie, and a reader has no way to catch it.
 */
export function currencyAffix(code: string | undefined | null): CurrencyAffix {
  if (code === undefined || code === null) return USD_AFFIX
  if (code === "") return NO_AFFIX

  const key = code.trim().toUpperCase()
  if (key === "") return NO_AFFIX
  const cached = AFFIX_CACHE.get(key)
  if (cached) return cached

  const affix = deriveAffix(key)
  AFFIX_CACHE.set(key, affix)
  return affix
}

/**
 * The non-breaking spaces Intl puts between a lettered code and the digits.
 *
 * U+00A0, or U+202F in some locales. Invisible in a diff and in a test
 * failure, so "AED 1,234" compared against "AED 1,234" reports a mismatch
 * with nothing to see. Built from char codes rather than written literally,
 * which also keeps the source free of characters no one can spot.
 */
const NBSP = new RegExp(
  `[${String.fromCharCode(0xa0)}${String.fromCharCode(0x202f)}]`,
  "g"
)

/** The parts of a formatted number that are the NUMBER, not decoration. */
const NUMERIC_PARTS = new Set(["integer", "group", "decimal", "fraction"])

function deriveAffix(code: string): CurrencyAffix {
  if (!/^[A-Z]{3}$/.test(code)) return NO_AFFIX
  try {
    const parts = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: code,
      // "$" and "₹" rather than "US$" and "₹" — and for a currency with no
      // symbol of its own, such as AED, Intl gives back the code itself.
      currencyDisplay: "narrowSymbol",
    }).formatToParts(1)

    let prefix = ""
    let suffix = ""
    let seenNumber = false
    for (const part of parts) {
      if (NUMERIC_PARTS.has(part.type)) {
        seenNumber = true
        continue
      }
      if (seenNumber) suffix += part.value
      else prefix += part.value
    }
    // Intl separates a lettered code from the digits with a NON-BREAKING space
    // (U+00A0, or U+202F for some locales). Invisible, and it makes every
    // comparison against "AED 1,234" fail for no reason a reader can see.
    // Normalised to an ordinary space, which is what the PDF side emits too.
    const plain = (text: string) => text.replace(NBSP, " ")
    return prefix || suffix
      ? { prefix: plain(prefix), suffix: plain(suffix) }
      : NO_AFFIX
  } catch {
    // RangeError for a code Intl does not recognise.
    return NO_AFFIX
  }
}

/**
 * Money, in the currency the column declares.
 *
 * The sign goes OUTSIDE the symbol — -$5M, never $-5M — and that holds for a
 * multi-character affix too: -AED 5.2M.
 */
export function money(value: number, compact = true, currency?: string | null): string {
  const { prefix, suffix } = currencyAffix(currency)
  const sign = value < 0 ? "-" : ""
  const abs = Math.abs(value)
  const body =
    compact && abs >= 1e3
      ? abbreviate(abs)
      : abs.toLocaleString("en-US", { maximumFractionDigits: abs % 1 ? 2 : 0 })
  return `${sign}${prefix}${body}${suffix}`
}

/** Axis tick / compact display, keyed on the value column's name. */
export function formatAxis(
  name: string | null | undefined,
  value: unknown,
  /** The panel's declared formats. An axis that ignores these contradicts the
   *  table underneath it: BUY_SELL_RATE_PCT declared as a plain number still
   *  printed 100%/75%/50%, because the name ends in _PCT and the classifier
   *  never saw the declaration. Declared wins, exactly as in formatCell. */
  formats?: ColumnFormats | null,
  /** The database's reporting currency, when it declares one. Applies only
   *  where the column itself has not stated a code. */
  defaultCurrency?: string | null
): string {
  if (typeof value !== "number") return String(value ?? "")
  const declared = name ? formats?.[name] : undefined
  // Decimals declared with no format ("auto" in the editor) are honoured on
  // their own, exactly as formatDeclared does for a table cell — otherwise the
  // axis and the column underneath it disagree about precision.
  if (declared && declared.format === undefined && declared.decimals !== undefined) {
    return withDecimals(value, declared.decimals)
  }
  switch (declared?.format ?? classify(name)) {
    case "percent":
      // Only a DECLARED precision overrides `percent`, which also trims
      // trailing zeros — "100.0%" on an axis reads as noise next to "100%".
      return declared?.decimals !== undefined
        ? `${withDecimals(value, declared.decimals)}%`
        : percent(value)
    case "currency":
      return money(value, true, resolveCurrency(declared, defaultCurrency))
    case "ratio":
      return NF.format(value)
    case "text":
      return String(value)
    case "number":
      // Declared decimals are honoured while the number is short enough to
      // print in full; past that an axis needs the abbreviation more than it
      // needs the precision.
      return declared?.decimals !== undefined && Math.abs(value) < 100000
        ? withDecimals(value, declared.decimals)
        : abbreviate(value)
    default:
      return abbreviate(value)
  }
}

/**
 * A column's DECLARED formatting, authored on the panel.
 *
 * The classifier below reads a column's NAME to guess its unit, which works
 * until a column is called TOTAL, AMT or OPENING_BAL — those lose their
 * currency and render as bare numbers. KPI tiles already solved this with an
 * authored FORMAT, and formatKpi carries the reason: "Debt Count" and "Debt
 * Outstanding" share a word and are a count and a currency respectively.
 *
 * Declared wins; anything undeclared falls back to the name classifier, so
 * dashboards saved before this render exactly as they did.
 */
export type ColumnFormat = {
  format?: "currency" | "percent" | "number" | "ratio" | "text" | "date"
  decimals?: number
  /** Display name for the header, without touching the SQL alias. */
  label?: string
  /**
   * ISO 4217 code, meaningful only alongside `format: "currency"`.
   *
   * ABSENT means US dollars — the default every dashboard written before this
   * relies on. EMPTY STRING is a different claim: the author does not know, or
   * the column mixes currencies, so no symbol is printed at all. Collapsing the
   * two would quietly restore dollars to a column that said it was not.
   */
  currency?: string
}

export type ColumnFormats = Record<string, ColumnFormat>

/** The header a column should show: its declared label, else its own name. */
export function columnLabel(
  name: string,
  formats?: ColumnFormats | null
): string {
  return formats?.[name]?.label?.trim() || name
}

/* Kept upper-case when a column name is made readable. Business shorthand a
   reader knows as letters, not words: "Gmv" and "Roas" read as typos. */
const ACRONYMS = new Set([
  "ADR", "AML", "AOV", "API", "ARI", "ARPU", "ARR", "ATV", "AUM", "B2B", "B2C", "BI",
  "BNPL", "CAC",
  "CBM", "CM", "COD", "COGS", "CPC", "CPM", "CSAT", "CTR", "D2C", "DIO", "DPD",
  "DPI", "DPO", "DSCR", "DSO", "EBIT", "EBITDA", "ECL", "ETA", "ETD", "FCL", "FX",
  "GCC", "GM", "GMV", "GOP", "GOPPAR", "HS", "ID", "IRR", "ISO", "KPI", "KSA", "KYB",
  "KYC", "LCL", "LFL", "LTC", "LTV", "MA", "MDR", "MER", "MICE", "MOM", "MPI", "MRR", "MTD", "NAV",
  "NOI", "NPL", "NPS", "OOS", "OTD", "OTIF", "PDP", "POD", "QOQ", "RFQ", "RGI",
  "ROAS", "ROI", "RPU", "SKU", "SLA", "TEU",
  "TPV", "TVPI", "UAE", "UK", "US", "USA", "VAT", "YOY", "YTD",
])
/* Terms with a conventional spelling that is neither upper nor title case. */
const SPELLED = new Map([
  ["FB", "F&B"],
  ["KG", "kg"],
  ["KM", "km"],
  ["KWH", "kWh"],
  ["MS", "ms"],
  ["REVPAR", "RevPAR"],
  ["SQFT", "sq ft"],
  ["TREVPAR", "TRevPAR"],
])
/* Joining words read better lower-case in a label — "Win Rate vs Target". */
const SMALL_WORDS = new Set(["AND", "AT", "BY", "FOR", "IN", "OF", "ON", "OR", "PER", "THE", "TO", "VS"])
/* A trailing currency code: the value itself already says "AED 5.2M". */
const CURRENCY_SUFFIXES = new Set(["AED", "BHD", "EUR", "GBP", "INR", "KWD", "OMR", "QAR", "SAR", "USD"])

/**
 * An SQL column name as a person would write it: NET_REVENUE_AED -> "Net
 * Revenue", CONTRIBUTION_MARGIN_PCT -> "Contribution Margin %".
 *
 * Only SQL-shaped names (upper-case, digits, underscores) are rewritten; any
 * other string is returned untouched, because it may already be a label.
 */
export function readableColumnName(name: string): string {
  if (!/^[A-Z0-9_]+$/.test(name) || !/[A-Z]/.test(name)) return name
  const tokens = name.split("_").filter(Boolean)
  if (tokens.length > 1 && CURRENCY_SUFFIXES.has(tokens[tokens.length - 1])) {
    tokens.pop()
  }
  const words = tokens.map((token, i) => {
    if (token === "PCT" || token === "PERCENT") return "%"
    if (token === "PP") return "pp"
    // A period length: 4WK -> 4-Week, 12M stays as it is (months or millions).
    const weeks = /^(\d+)WK$/.exec(token)
    if (weeks) return `${weeks[1]}-Week`
    if (token === "WK") return "Week"
    const spelled = SPELLED.get(token)
    if (spelled) return spelled
    if (ACRONYMS.has(token) || CURRENCY_SUFFIXES.has(token) || /\d/.test(token)) {
      return token
    }
    // A plural acronym keeps its letters and gains a lower-case s: RFQS -> RFQs.
    if (token.length > 2 && token.endsWith("S") && ACRONYMS.has(token.slice(0, -1))) {
      return `${token.slice(0, -1)}s`
    }
    if (i > 0 && SMALL_WORDS.has(token)) return token.toLowerCase()
    return token.charAt(0) + token.slice(1).toLowerCase()
  })
  return words.join(" ")
}

/**
 * The name a chart legend, tooltip or table header shows for a series.
 *
 * A declared label always wins. Otherwise a column name is made readable —
 * but only a COLUMN name: a chart split by a category draws one series per
 * VALUE ("noon", "UAE"), and those are data, which must be shown exactly as
 * they are. Pass the panel's columns so the two can be told apart.
 *
 * Display only. Formats, colours and clicks stay keyed on the real column, so
 * nothing that looks a column up by name is affected.
 */
export function seriesLabel(
  name: string,
  formats?: ColumnFormats | null,
  columns?: readonly string[] | null
): string {
  if (columns && !columns.includes(name)) return name
  const declared = formats?.[name]?.label?.trim()
  return declared || readableColumnName(name)
}

function withDecimals(value: number, decimals: number | undefined): string {
  return decimals === undefined
    ? NF.format(value)
    : value.toLocaleString("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })
}

/**
 * A table cell under an explicitly declared format.
 *
 * Returns null when the column declares nothing usable, so the caller can fall
 * through to the name-based path rather than this having to know about it.
 */
/**
 * Which currency a number is in: what the column declares, else the database's.
 *
 * Tested with `!== undefined` rather than truthiness on purpose. An explicitly
 * EMPTY code means "this money has no nameable currency" — a total that mixes
 * them — and that is a decision, not an absence. Under a truthy test it would
 * fall through to the database default and print a symbol the author had
 * deliberately removed.
 */
function resolveCurrency(
  spec: ColumnFormat | undefined,
  defaultCurrency: string | null | undefined
): string | undefined {
  if (spec?.currency !== undefined) return spec.currency
  return defaultCurrency ?? undefined
}

function formatDeclared(
  spec: ColumnFormat,
  value: unknown,
  defaultCurrency?: string | null
): string | null {
  if (typeof value !== "number") return null
  const { format, decimals } = spec
  switch (format) {
    case "currency": {
      const { prefix, suffix } = currencyAffix(
        resolveCurrency(spec, defaultCurrency)
      )
      const sign = value < 0 ? "-" : ""
      const abs = Math.abs(value)
      return `${sign}${prefix}${withDecimals(abs, decimals ?? (abs % 1 ? 2 : 0))}${suffix}`
    }
    case "percent":
      return `${withDecimals(value, decimals ?? percentDecimals(value))}%`
    case "ratio":
      return withDecimals(value, decimals ?? 2)
    case "number":
      return withDecimals(value, decimals)
    case "text":
      return String(value)
    default:
      // No format declared, but decimals might be — honour them alone.
      return decimals === undefined ? null : withDecimals(value, decimals)
  }
}

/**
 * How coarse a column of dates actually is, so an axis can say "Jan 2025"
 * rather than "2025-01-01".
 *
 * Monthly and yearly series are stored as the FIRST DAY of their period —
 * that is the convention the SQL produces — so every label carried a day
 * number that means nothing and cost the width that would have let more
 * labels fit. The granularity is read off the data rather than declared,
 * because the column name does not carry it.
 *
 * Deliberately conservative: anything that is not wholly one shape is treated
 * as daily and left alone. Quarterly is NOT inferred — monthly data that
 * happens to cover Jan/Apr/Jul/Oct is indistinguishable from it, and a wrong
 * "Q1" is worse than a right "Jan".
 */
export type DateGranularity = "year" | "month" | "day"

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$/

export function detectDateGranularity(
  values: readonly unknown[]
): DateGranularity | null {
  let seen = 0
  let allFirstOfMonth = true
  let allFirstOfYear = true

  for (const value of values) {
    if (value === null || value === undefined || value === "") continue
    const match = ISO_DATE.exec(String(value).trim())
    if (!match) return null // one non-date and the column is not a date column
    seen++
    const [, , month, day] = match
    if (day !== "01") {
      allFirstOfMonth = false
      allFirstOfYear = false
    } else if (month !== "01") {
      allFirstOfYear = false
    }
  }

  if (seen === 0) return null
  if (allFirstOfYear) return "year"
  if (allFirstOfMonth) return "month"
  return "day"
}

/**
 * One date label at the given granularity. Unparseable values pass through.
 *
 * Numeric MM-YYYY rather than "Feb 2025": a reader does not have to map a
 * month name back to its position, which is the whole job an axis label has
 * to do at a glance. The table keeps the full ISO date, where the exact day
 * is what someone is checking.
 */
export function formatDateLabel(
  value: unknown,
  granularity: DateGranularity | null,
  /** Axes drop the year on a daily series to save tick width. A table has no
   *  such pressure and a bare "02-05" there is genuinely ambiguous, so it
   *  keeps the year. */
  compact = true,
  /** The series crosses a calendar year. A compact day label then keeps a
   *  two-digit year: 80 weeks from Feb 2025 labelled "02-03" twice over gave
   *  no way to tell February 2025 from February 2026. */
  spansYears = false
): string {
  const text = String(value ?? "")
  if (!granularity) return text
  const match = ISO_DATE.exec(text.trim())
  if (!match) return text
  const [, year, month, day] = match
  switch (granularity) {
    case "year":
      return year
    case "month":
      return `${month}-${year}`
    default:
      if (!compact) return `${month}-${day}-${year}`
      return spansYears ? `${month}-${day}-${year.slice(2)}` : `${month}-${day}`
  }
}

/**
 * Granularity for each column of a result, so a table can show a month column
 * as a month.
 *
 * A column called SALE MONTH holding "2025-02-01" is showing a day that is a
 * storage convention, not a fact — the axis had already stopped doing that
 * while the table beside it carried on.
 */
export function columnDateGrains(
  columns: readonly string[],
  rows: readonly (readonly unknown[])[]
): (DateGranularity | null)[] {
  return columns.map((_, index) =>
    detectDateGranularity(rows.map((row) => row[index]))
  )
}

/** Full-precision table cell, keyed on its column name. */
export function formatCell(
  name: string | null | undefined,
  value: unknown,
  formats?: ColumnFormats | null,
  /** Set for a column that holds dates, so a monthly series reads 02-2025
   *  here exactly as it does on the axis. A declared format still wins. */
  dateGrain?: DateGranularity | null,
  /** The database's reporting currency — see formatAxis. */
  defaultCurrency?: string | null
): string {
  if (value === null || value === undefined) return ""
  const declared = name ? formats?.[name] : undefined
  if (declared) {
    const shown = formatDeclared(declared, value, defaultCurrency)
    if (shown !== null) return shown
  }
  if (dateGrain) return formatDateLabel(value, dateGrain, false)
  if (value === true || value === "true") return "Yes"
  if (value === false || value === "false") return "No"
  if (typeof value !== "number") return String(value)
  switch (classify(name)) {
    case "percent":
      return percent(value)
    case "currency":
      // Undeclared: the classifier guessed a currency from the column NAME, so
      // there is no code on the column itself. The database's own code applies
      // if it has one; with none this is dollars, exactly as before.
      return money(value, false, defaultCurrency ?? undefined)
    default:
      return NF.format(value)
  }
}

/**
 * KPI tile value from the metric's DECLARED format — the FORMAT column the
 * planner authors. Authoritative: the renderer must not infer a unit from the
 * metric's label, because "Debt Count" and "Debt Outstanding" share a word and
 * are a count and a currency respectively.
 *
 * An unknown/absent FORMAT falls back to the name-based classifier so
 * dashboards saved before FORMAT existed still render.
 */
export function formatKpi(
  format: unknown,
  metricLabel: string,
  value: unknown,
  /** The database's reporting currency. A FORMAT that names a code — or
   *  names an empty one with "currency:" — still wins over it. */
  defaultCurrency?: string | null
): string {
  if (typeof value !== "number") return String(value ?? "")
  /* A KPI row is METRIC / VALUE / FORMAT and has no column_formats to hang a
     currency on, so the code rides with the word: "currency:AED". Bare
     "currency" stays dollars, and "currency:" with nothing after it means no
     symbol — the same three states the column field has. */
  const raw = String(format ?? "").trim()
  const colon = raw.indexOf(":")
  const currency = colon >= 0 ? raw.slice(colon + 1).trim() : undefined
  switch ((colon >= 0 ? raw.slice(0, colon) : raw).toLowerCase()) {
    case "currency":
      // `currency` is undefined only when FORMAT carried no colon at all.
      // "currency:" yields "" — a deliberate no-symbol — and must survive.
      return money(value, true, currency ?? defaultCurrency ?? undefined)
    case "percent":
      return percent(value) // already whole-number percent
    case "count":
      return NF0.format(value) // integer tally, never decimals
    case "ratio":
      return NF.format(value)
    default:
      return formatAxis(metricLabel, value, undefined, defaultCurrency)
  }
}

/**
 * Fixed categorical order — never cycled, so a series keeps its colour when
 * the data is filtered or re-run. Beyond the palette we fall back to grey
 * rather than wrapping round to the first hue and implying a false pairing.
 *
 * Palette is finance-forward (steel / teal / warm accents) rather than a
 * generic rainbow, so multi-series boards read as one product.
 */
export const SERIES_COLORS = [
  "#557AA2",
  "#B86D46",
  "#479E94",
  "#CAA14E",
  "#786D95",
  "#5F856B",
  "#C2817C",
  "#6D7883",
] as const

const OVERFLOW_COLOR = "#8E8C87"

/**
 * Status / payment labels that should keep a stable meaning across dashboards
 * (Paid stays green even when it is not series index 0).
 */
const SEMANTIC_SERIES: Array<{ match: RegExp; color: string }> = [
  { match: /\bpaid\b|\bcollected\b|\bcurrent\b|\bcompliant\b|\bperform/i, color: "#479E94" },
  { match: /\boutstanding\b|\binvoiced\b|\bactive\b/i, color: "#557AA2" },
  { match: /\boverdue\b|\bdelinq|\bpast[_\s-]?due\b|\blate\b/i, color: "#B86D46" },
  { match: /\bdisputed\b|\bbreach|\bcritical\b|\bwatch/i, color: "#C2817C" },
  { match: /\bpartial|\bpending|\bmonitoring|\bwarning/i, color: "#CAA14E" },
  { match: /\bvacant\b|\bempty\b/i, color: "#6D7883" },
  { match: /\boccupied\b|\bleased\b/i, color: "#5F856B" },
]

/**
 * Named palettes a panel can choose instead of the default series colours.
 *
 * Each is eight colours that stay distinguishable from their neighbours on
 * both a light and a dark ground — a palette whose third and fourth series
 * look alike is worse than no choice at all.
 *
 * The key must match `PALETTE_NAMES` in the backend, which drops any name it
 * does not know before the value can reach a renderer with no colours for it.
 */
export const PALETTES: Record<string, readonly string[]> = {
  ocean: [
    "#397095", "#6DB1B1", "#295A7D", "#9CB7CC",
    "#4B9BA5", "#617BA1", "#B2D4D6", "#34837F",
  ],
  ember: [
    "#AE663F", "#CDA866", "#89483B", "#C3836D",
    "#A5734B", "#753F34", "#DFC99F", "#B7584B",
  ],
  meadow: [
    "#557B4E", "#A1B065", "#49886F", "#B99E49",
    "#779A57", "#3B7360", "#C9C597", "#87A779",
  ],
  slate: [
    "#58697C", "#988A83", "#485563", "#87929F",
    "#6E7A6D", "#8F8490", "#ADA8A2", "#5F787E",
  ],
}

/**
 * The colour for an element that has to stand apart from a field of others.
 *
 * A combo's trend line is the case that forced this: inside a themed palette
 * the line took the next series colour, which in a green palette is another
 * green — a thin line drawn over thick bars of almost the same colour. The
 * accent is deliberately from a different hue family.
 */
export const PALETTE_ACCENTS: Record<string, string> = {
  ocean: "#BF8852",
  ember: "#3C7D94",
  meadow: "#B56949",
  slate: "#A87665",
  // The default set already contrasts, so this is the colour a combo
  // line has always used — nothing changes for a panel with no palette.
  default: "#B86D46",
}

/** Palette choices for the editor, in the order they are offered. */
export const PALETTE_OPTIONS = [
  { value: "", label: "Default" },
  { value: "ocean", label: "Ocean" },
  { value: "ember", label: "Ember" },
  { value: "meadow", label: "Meadow" },
  { value: "slate", label: "Slate" },
] as const

/**
 * The colour for one element that must read against a field of others — the
 * trend line over a combo's bars.
 *
 * `used` is the colours already on the chart. The accent is taken unless it is
 * one of them, in which case the next unused series colour is. That fallback
 * is what keeps a panel with NO palette looking exactly as it always has:
 * with one bar series the default accent IS the second series colour, and with
 * more it steps past the bars the same way the old index arithmetic did.
 */
export function accentColor(
  palette: string | null | undefined,
  used: readonly string[] = []
): string {
  const accent = PALETTE_ACCENTS[palette || "default"]
  if (accent && !used.includes(accent)) return accent

  const set = palette ? PALETTES[palette] : undefined
  const pool = set ?? SERIES_COLORS
  return pool.find((colour) => !used.includes(colour)) ?? OVERFLOW_COLOR
}

/**
 * Series colour by index, with optional label for semantic status colours.
 *
 * A named palette wins OUTRIGHT, semantic rules included. Keeping "Paid" green
 * inside a chosen warm palette produces a chart that is mostly ember with one
 * green bar in it, which reads as a rendering fault rather than a decision.
 * With no palette named — every panel saved before this existed — the
 * behaviour is exactly what it always was.
 */
export function seriesColor(
  index: number,
  label?: string | null,
  palette?: string | null
): string {
  const chosen = palette ? PALETTES[palette] : undefined
  if (chosen) return extendedColor(chosen, index)

  if (label) {
    const text = String(label)
    for (const entry of SEMANTIC_SERIES) {
      if (entry.match.test(text)) return entry.color
    }
  }
  return extendedColor(SERIES_COLORS, index)
}

/**
 * The index-th colour of a set, continuing past its end with new colours.
 *
 * Past the end, a named palette used to wrap — series 9 took series 1's
 * colour — and the default set gave every extra series the same grey. An
 * 11-category scatter then had three points in identical grey and no way to
 * tell them apart. Each later lap takes the set's own colours with the hue
 * turned and the lightness shifted, so the extras stay in the palette's
 * family but never repeat one another.
 */
function extendedColor(set: readonly string[], index: number): string {
  if (index < set.length) return set[index]
  const lap = Math.floor(index / set.length)
  const hsl = hexToHsl(set[index % set.length])
  if (!hsl) return OVERFLOW_COLOR
  const hue = (hsl.h + lap * 137.5) % 360
  const lightness = Math.min(70, Math.max(35, hsl.l + (lap % 2 ? 12 : -12)))
  return `hsl(${hue.toFixed(0)} ${hsl.s.toFixed(0)}% ${lightness.toFixed(0)}%)`
}

function hexToHsl(hex: string): { h: number; s: number; l: number } | null {
  const match = /^#([0-9a-f]{6})$/i.exec(hex)
  if (!match) return null
  const n = parseInt(match[1], 16)
  const r = ((n >> 16) & 255) / 255
  const g = ((n >> 8) & 255) / 255
  const b = (n & 255) / 255
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const l = (max + min) / 2
  if (max === min) return { h: 0, s: 0, l: l * 100 }
  const d = max - min
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
  const h =
    max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4
  return { h: h * 60, s: s * 100, l: l * 100 }
}

/**
 * Continuous blue scale for heatmaps — light = low, deep = high.
 * Returns an rgb string; `t` is 0..1.
 */
export function heatColor(t: number): string {
  const x = Math.max(0, Math.min(1, t))
  // Blend #E8F1FA → #3B6EA5
  const r = Math.round(232 + (59 - 232) * x)
  const g = Math.round(241 + (110 - 241) * x)
  const b = Math.round(250 + (165 - 250) * x)
  return `rgb(${r}, ${g}, ${b})`
}

/** Max heatmap rows kept after ranking by intensity — beyond this cells shrink unreadably. */
export const HEATMAP_MAX_ROWS = 15

/**
 * Leading number from an aging / bucket label so "0-30", "31-60", "90+" sort
 * in bucket order instead of alphabetically ("90+" before "0-30").
 */
function leadingBucketNumber(label: string): number | null {
  const m = String(label).trim().match(/^(\d+)/)
  if (!m) return null
  const n = Number(m[1])
  return Number.isFinite(n) ? n : null
}

const MONTH_NAMES = [
  "january", "february", "march", "april", "may", "june",
  "july", "august", "september", "october", "november", "december",
]

/** 1-12 for a month name or abbreviation ("Feb", "Sept", "October"), else 0. */
function monthNumber(word: string): number {
  const lower = word.toLowerCase()
  if (lower.length < 3) return 0
  const at = MONTH_NAMES.findIndex((name) => name.startsWith(lower))
  return at + 1
}

/** A four-digit year, or a two-digit one read as 20xx ("Q1 '25"). */
function fullYear(text: string): number {
  const year = Number(text)
  return text.length === 2 ? 2000 + year : year
}

/**
 * A period label as a number that sorts by time, with the kind of period it
 * is, or null when the label is not one.
 *
 * Heatmap axes sort their labels, so a period written as text landed in
 * alphabetical order whatever order the query returned: "Q1-2025, Q1-2026,
 * Q2-2025…" and "Apr-2025, Aug-2025, Dec-2025…". Recognised, either way round
 * and with any separator: quarters and halves ("Q1-2025", "2025 Q1",
 * "H2 2025"), month names ("Feb-2025", "February 2025", "2025 Feb"), and
 * numeric months ("2025-02", "02-2025", "02/2025").
 */
function periodKey(label: string): { kind: string; key: number } | null {
  const text = label.trim()
  let m = /^([QH])([1-4])[\s\-_/.]*'?(\d{4}|\d{2})$/i.exec(text)
  if (m) {
    return { kind: m[1].toUpperCase(), key: fullYear(m[3]) * 10 + Number(m[2]) }
  }
  m = /^(\d{4})[\s\-_/.]*([QH])([1-4])$/i.exec(text)
  if (m) {
    return { kind: m[2].toUpperCase(), key: Number(m[1]) * 10 + Number(m[3]) }
  }
  m = /^([a-z]{3,9})\.?[\s\-_/.,]*'?(\d{4}|\d{2})$/i.exec(text)
  if (m && monthNumber(m[1])) {
    return { kind: "M", key: fullYear(m[2]) * 100 + monthNumber(m[1]) }
  }
  m = /^(\d{4})[\s\-_/.,]*([a-z]{3,9})\.?$/i.exec(text)
  if (m && monthNumber(m[2])) {
    return { kind: "M", key: Number(m[1]) * 100 + monthNumber(m[2]) }
  }
  m = /^(\d{4})[-/.](\d{1,2})$/.exec(text)
  if (m && Number(m[2]) >= 1 && Number(m[2]) <= 12) {
    return { kind: "M", key: Number(m[1]) * 100 + Number(m[2]) }
  }
  m = /^(\d{1,2})[-/.](\d{4})$/.exec(text)
  if (m && Number(m[1]) >= 1 && Number(m[1]) <= 12) {
    return { kind: "M", key: Number(m[2]) * 100 + Number(m[1]) }
  }
  return null
}

/**
 * Stable order for heatmap axes. Periods — when every label is the same kind
 * of period — go by time. Otherwise: aging buckets by leading day count, then
 * numeric-looking labels, then locale string compare.
 */
export function sortHeatLabels(labels: string[]): string[] {
  const periods = labels.map(periodKey)
  const kind = periods[0]?.kind
  if (
    labels.length > 0 &&
    periods.every((period) => period !== null && period.kind === kind)
  ) {
    return labels
      .map((label, i) => ({ label, key: periods[i]!.key }))
      .sort((a, b) => a.key - b.key)
      .map(({ label }) => label)
  }
  return [...labels].sort((a, b) => {
    const na = leadingBucketNumber(a)
    const nb = leadingBucketNumber(b)
    if (na !== null && nb !== null && na !== nb) return na - nb
    if (na !== null && nb === null) return -1
    if (na === null && nb !== null) return 1
    const fa = Number(a)
    const fb = Number(b)
    if (Number.isFinite(fa) && Number.isFinite(fb) && fa !== fb) return fa - fb
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" })
  })
}

/**
 * Keep the strongest rows (by total absolute intensity) so a client×aging
 * matrix with dozens of clients still fits on screen.
 */
export function rankHeatRows(
  rowLabels: string[],
  colLabels: string[],
  cells: Map<string, number>,
  maxRows: number = HEATMAP_MAX_ROWS
): string[] {
  if (rowLabels.length <= maxRows) return rowLabels
  const scored = rowLabels.map((r) => {
    let total = 0
    for (const c of colLabels) {
      const v = cells.get(`${r}\0${c}`)
      if (typeof v === "number" && Number.isFinite(v)) total += Math.abs(v)
    }
    return { r, total }
  })
  scored.sort((a, b) => b.total - a.total || a.r.localeCompare(b.r))
  const kept = new Set(scored.slice(0, maxRows).map((s) => s.r))
  // Preserve encounter order among the survivors so the planner's sort is not
  // scrambled into pure magnitude order when that was intentional.
  return rowLabels.filter((r) => kept.has(r))
}

/** Column index by name, case-insensitive. -1 when absent. */
export function columnIndex(
  columns: string[],
  name: string | null | undefined
): number {
  if (!name) return -1
  const target = String(name).toUpperCase()
  return columns.findIndex((c) => String(c).toUpperCase() === target)
}

export function isNumericColumn(rows: unknown[][], index: number): boolean {
  if (index < 0) return false
  const values = rows
    .map((r) => r[index])
    .filter((v) => v !== null && v !== undefined)
  return values.length > 0 && values.every((v) => typeof v === "number")
}

/**
 * Rows -> Recharts data, preserving SQL row order exactly.
 *
 * Order is deliberate, not incidental: panel SQL does its own ORDER BY (a
 * quarter label sorts by its companion date column, not alphabetically), so
 * re-sorting here would silently scramble every time series.
 */
export function toChartData(
  columns: string[],
  rows: unknown[][]
): Array<Record<string, unknown>> {
  return rows.map((row) => {
    const item: Record<string, unknown> = {}
    columns.forEach((col, i) => {
      item[col] = row[i]
    })
    return item
  })
}
