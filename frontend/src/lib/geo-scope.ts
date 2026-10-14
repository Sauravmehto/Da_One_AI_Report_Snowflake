/**
 * Deciding whether a map panel is a US map or a world map.
 *
 * The panel is not told which to draw — it works it out from the values. That
 * is what makes "one region zooms in, three regions zoom out" fall out for
 * free: there is no region parameter for a planner to get wrong, and a saved
 * dashboard whose chart_type says `geo_us_states` still renders correctly when
 * the query behind it starts returning countries.
 *
 * The whole difficulty is that two-letter codes are ambiguous. IN is India and
 * Indiana; CA is Canada and California; DE, AL, MO, MD, MT, NE, PA and GE all
 * collide the same way. Counting matches per interpretation would let a single
 * ambiguous value swing the decision, so the rule below only ever counts
 * UNAMBIGUOUS evidence — a value that can be read one way and not the other.
 * "Germany" can only be a country; "Texas" can only be a state; "CA" votes for
 * neither and is resolved afterwards by whichever map won.
 */

import { matchCountry } from "@/lib/geo-countries"
import { matchState } from "@/lib/us-states"

export type GeoScope = "us_states" | "countries"

export type GeoScopeHints = {
  /** What the planner asked for. Weaker than the data, stronger than nothing. */
  chartType?: string | null
  /** The x column's name, e.g. PROPERTY_STATE or GEOGRAPHY. */
  column?: string | null
}

/* Column names that say what grain the values are, when the values cannot.
   Underscores are separators here, so PROPERTY_STATE reads as two words — a
   plain \b would not match it, since \b sees "_" as a word character. "region"
   is deliberately absent from the country pattern: RE_PROPERTIES has a
   STATE_REGION column that holds either, so the word settles nothing. */
const COUNTRY_COLUMN = /\b(countr\w*|geograph\w*|domicil\w*|nation|port|airport|market)\b/
const STATE_COLUMN = /\b(state|states|province|provinces)\b/

function columnWords(column: string): string {
  return column.replace(/[^a-zA-Z]+/g, " ").toLowerCase().trim()
}

export type GeoEvidence = {
  /** Values that can only be a US state. */
  onlyState: number
  /** Values that can only be a country. */
  onlyCountry: number
  /** Values both tables claim — two-letter codes, and Georgia. */
  ambiguous: number
  /** Values neither table claims: regions, cities we don't know, junk. */
  neither: number
}

/** What the distinct values say about which map they belong to. */
export function geoEvidence(values: unknown[]): GeoEvidence {
  const seen = new Set<string>()
  const evidence: GeoEvidence = {
    onlyState: 0,
    onlyCountry: 0,
    ambiguous: 0,
    neither: 0,
  }

  for (const value of values) {
    if (value === null || value === undefined) continue
    const raw = String(value).trim()
    if (raw === "" || seen.has(raw)) continue
    seen.add(raw)

    const isState = matchState(raw) !== undefined
    const isCountry = matchCountry(raw) !== undefined

    if (isState && isCountry) evidence.ambiguous += 1
    else if (isState) evidence.onlyState += 1
    else if (isCountry) evidence.onlyCountry += 1
    else evidence.neither += 1
  }
  return evidence
}

/**
 * The map these values want.
 *
 * Data first, then the planner's declared type, then the column's name, then
 * the US — which is where this panel started and what the semantic layer's
 * bare two-letter columns (PROPERTY_STATE) actually hold.
 */
export function resolveGeoScope(
  values: unknown[],
  hints: GeoScopeHints = {}
): GeoScope {
  const { onlyState, onlyCountry } = geoEvidence(values)

  // A column holding even one country that is not a state cannot be a US map,
  // and vice versa. When both appear — "Germany" next to "Texas" — only the
  // world map can show both, so it wins.
  if (onlyCountry > 0) return "countries"
  if (onlyState > 0) return "us_states"

  const chartType = (hints.chartType ?? "").toLowerCase()
  if (chartType === "geo_countries") return "countries"
  if (chartType === "geo_us_states") return "us_states"

  const column = columnWords(hints.column ?? "")
  if (COUNTRY_COLUMN.test(column)) return "countries"
  if (STATE_COLUMN.test(column)) return "us_states"

  return "us_states"
}
