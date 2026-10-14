/**
 * Matching a data column to a US state on the map.
 *
 * The boundary file (us-atlas) identifies states by FIPS code and full name;
 * warehouse columns almost always hold the two-letter code — the semantic
 * layer describes STATE as "Two-letter state code (TEXT). Primary filter". So
 * a lookup has to bridge the two, and accept either spelling from the data.
 *
 * Matching is deliberately forgiving on case and surrounding space, because a
 * silent miss on a map is invisible: the state simply renders unshaded, which
 * reads as "no data" rather than "we could not match this row". `unmatched()`
 * exists so the panel can say so out loud instead.
 */

export type UsState = {
  /** FIPS code, as used by the boundary file. */
  fips: string
  /** USPS two-letter code. */
  code: string
  name: string
}

/* FIPS / USPS / name for the 50 states plus DC — the 51 the boundary file
   carries. Puerto Rico and the territories are not in states-albers-10m. */
export const US_STATES: UsState[] = [
  { fips: "01", code: "AL", name: "Alabama" },
  { fips: "02", code: "AK", name: "Alaska" },
  { fips: "04", code: "AZ", name: "Arizona" },
  { fips: "05", code: "AR", name: "Arkansas" },
  { fips: "06", code: "CA", name: "California" },
  { fips: "08", code: "CO", name: "Colorado" },
  { fips: "09", code: "CT", name: "Connecticut" },
  { fips: "10", code: "DE", name: "Delaware" },
  { fips: "11", code: "DC", name: "District of Columbia" },
  { fips: "12", code: "FL", name: "Florida" },
  { fips: "13", code: "GA", name: "Georgia" },
  { fips: "15", code: "HI", name: "Hawaii" },
  { fips: "16", code: "ID", name: "Idaho" },
  { fips: "17", code: "IL", name: "Illinois" },
  { fips: "18", code: "IN", name: "Indiana" },
  { fips: "19", code: "IA", name: "Iowa" },
  { fips: "20", code: "KS", name: "Kansas" },
  { fips: "21", code: "KY", name: "Kentucky" },
  { fips: "22", code: "LA", name: "Louisiana" },
  { fips: "23", code: "ME", name: "Maine" },
  { fips: "24", code: "MD", name: "Maryland" },
  { fips: "25", code: "MA", name: "Massachusetts" },
  { fips: "26", code: "MI", name: "Michigan" },
  { fips: "27", code: "MN", name: "Minnesota" },
  { fips: "28", code: "MS", name: "Mississippi" },
  { fips: "29", code: "MO", name: "Missouri" },
  { fips: "30", code: "MT", name: "Montana" },
  { fips: "31", code: "NE", name: "Nebraska" },
  { fips: "32", code: "NV", name: "Nevada" },
  { fips: "33", code: "NH", name: "New Hampshire" },
  { fips: "34", code: "NJ", name: "New Jersey" },
  { fips: "35", code: "NM", name: "New Mexico" },
  { fips: "36", code: "NY", name: "New York" },
  { fips: "37", code: "NC", name: "North Carolina" },
  { fips: "38", code: "ND", name: "North Dakota" },
  { fips: "39", code: "OH", name: "Ohio" },
  { fips: "40", code: "OK", name: "Oklahoma" },
  { fips: "41", code: "OR", name: "Oregon" },
  { fips: "42", code: "PA", name: "Pennsylvania" },
  { fips: "44", code: "RI", name: "Rhode Island" },
  { fips: "45", code: "SC", name: "South Carolina" },
  { fips: "46", code: "SD", name: "South Dakota" },
  { fips: "47", code: "TN", name: "Tennessee" },
  { fips: "48", code: "TX", name: "Texas" },
  { fips: "49", code: "UT", name: "Utah" },
  { fips: "50", code: "VT", name: "Vermont" },
  { fips: "51", code: "VA", name: "Virginia" },
  { fips: "53", code: "WA", name: "Washington" },
  { fips: "54", code: "WV", name: "West Virginia" },
  { fips: "55", code: "WI", name: "Wisconsin" },
  { fips: "56", code: "WY", name: "Wyoming" },
]

const BY_FIPS = new Map(US_STATES.map((s) => [s.fips, s]))

/** Every spelling a data value might use, pointing at its state. */
const BY_VALUE = new Map<string, UsState>()
for (const state of US_STATES) {
  BY_VALUE.set(state.code.toLowerCase(), state)
  BY_VALUE.set(state.name.toLowerCase(), state)
  // A FIPS code arriving as a number has usually lost its leading zero.
  BY_VALUE.set(state.fips, state)
  BY_VALUE.set(String(Number(state.fips)), state)
}
// The one alias worth carrying: DC is written half a dozen ways.
BY_VALUE.set("washington dc", BY_FIPS.get("11")!)
BY_VALUE.set("washington, d.c.", BY_FIPS.get("11")!)
BY_VALUE.set("d.c.", BY_FIPS.get("11")!)

export function stateByFips(fips: string): UsState | undefined {
  return BY_FIPS.get(String(fips).padStart(2, "0"))
}

/** The state a data value names, by code, full name or FIPS. */
export function matchState(value: unknown): UsState | undefined {
  if (value === null || value === undefined) return undefined
  return BY_VALUE.get(String(value).trim().toLowerCase())
}

/** Values that matched no state, so the panel can say so rather than hide it. */
export function unmatched(values: unknown[]): string[] {
  const missed: string[] = []
  for (const value of values) {
    if (value === null || value === undefined || String(value).trim() === "")
      continue
    if (!matchState(value)) missed.push(String(value))
  }
  return [...new Set(missed)]
}
