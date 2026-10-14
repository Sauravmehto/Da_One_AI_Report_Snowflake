/**
 * Choropleths: US states, or countries anywhere in the world.
 *
 * The panel is not told which map to draw. It reads the x values, works out
 * whether they are US states or countries (see `geo-scope.ts`), and for
 * countries FITS THE PROJECTION to the countries that actually matched. That
 * one mechanism is the whole feature: data about India draws India and its
 * neighbours, data about the UAE and India draws the Gulf and South Asia, and
 * data about the US, Germany and India draws the world — with no region
 * parameter for anyone, model or human, to get wrong.
 *
 * The failure worth designing against is a silent one: a value that matches no
 * place simply leaves that shape unshaded, which reads as "nothing there"
 * rather than "we could not match this". So unmatched values are counted and
 * reported under the map instead of disappearing. Regions like "Europe" land
 * there deliberately — shading forty-four countries with one number would
 * invent per-country data that does not exist.
 *
 * Both boundary files are loaded on demand. Together they are most of a
 * megabyte of geometry, and a dashboard with no map on it should not pay for
 * them.
 */

import * as React from "react"
import {
  geoBounds,
  geoEqualEarth,
  geoPath,
  type GeoGeometryObjects,
  type GeoPath,
  type GeoPermissibleObjects,
} from "d3-geo"
import type { Feature, Geometry } from "geojson"
import { feature } from "topojson-client"
import type { Topology } from "topojson-specification"

import {
  axisFormat,
  dataLabelsFor,
  formatLabelValue,
  labelIndices,
  type AxisFormat,
  type DataLabels,
} from "@/lib/chart-standard"
import { columnIndex, formatAxis, formatCell, heatColor } from "@/lib/dashboard-format"
import { countryById, matchCountry } from "@/lib/geo-countries"
import { geoEvidence, resolveGeoScope } from "@/lib/geo-scope"
import { matchState, stateByFips, unmatched } from "@/lib/us-states"
import type { DashboardPanel } from "@/components/dashboard/dashboard-panel"

type PlaceFeature = Feature<Geometry, { name: string }>

/* ------------------------------------------------------------------ atlases */

type UsAtlas = { features: PlaceFeature[]; viewBox: string }
type WorldAtlas = { features: PlaceFeature[] }

/**
 * The viewBox, measured from the geometry rather than assumed.
 *
 * The usual "0 0 975 610" is wrong for the us-atlas file: its Alaska inset
 * starts at x = -58, so a hardcoded box clips it. Nothing errors — Alaska is
 * simply missing a chunk, which is easy to miss and impossible to explain
 * later. Measuring also survives a different atlas version.
 */
function measuredViewBox(features: PlaceFeature[], path: GeoPath): string {
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  for (const f of features) {
    const [[fx0, fy0], [fx1, fy1]] = path.bounds(f as GeoPermissibleObjects)
    x0 = Math.min(x0, fx0)
    y0 = Math.min(y0, fy0)
    x1 = Math.max(x1, fx1)
    y1 = Math.max(y1, fy1)
  }
  const pad = 4
  return `${x0 - pad} ${y0 - pad} ${x1 - x0 + pad * 2} ${y1 - y0 + pad * 2}`
}

function toFeatures(topology: Topology, key: string): PlaceFeature[] {
  return (
    feature(topology, topology.objects[key]) as unknown as {
      features: PlaceFeature[]
    }
  ).features
}

/* Parsed once per page, not per render: converting the topology is the most
   expensive thing either map does, and two panels on one dashboard should
   share the result. */
let usAtlas: UsAtlas | null = null
let usPending: Promise<UsAtlas> | null = null

function loadUsAtlas(): Promise<UsAtlas> {
  usPending ??= import("us-atlas/states-albers-10m.json").then((module) => {
    // The PRE-PROJECTED file, so there is no runtime projection and Alaska and
    // Hawaii already sit in their usual insets.
    const features = toFeatures(module.default as unknown as Topology, "states")
    usAtlas = { features, viewBox: measuredViewBox(features, geoPath()) }
    return usAtlas
  })
  return usPending
}

let worldAtlas: WorldAtlas | null = null
let worldPending: Promise<WorldAtlas> | null = null

function loadWorldAtlas(): Promise<WorldAtlas> {
  // 50m rather than 110m: the 110m file drops Singapore, Hong Kong, Bahrain,
  // Malta and Mauritius, which between them are half the domiciles and ports
  // this data talks about. A map missing them would be worse than no map.
  worldPending ??= import("world-atlas/countries-50m.json").then((module) => {
    worldAtlas = {
      features: toFeatures(module.default as unknown as Topology, "countries"),
    }
    return worldAtlas
  })
  return worldPending
}

function useAtlas<T>(cached: T | null, load: () => Promise<T>): T | null {
  const [atlas, setAtlas] = React.useState<T | null>(cached)
  React.useEffect(() => {
    if (atlas) return
    let live = true
    load().then(
      (loaded) => {
        if (live) setAtlas(loaded)
      },
      () => {
        /* A missing boundary file leaves the skeleton up rather than throwing
           away the whole dashboard. */
      }
    )
    return () => {
      live = false
    }
  }, [atlas, load])
  return atlas
}

/* -------------------------------------------------------------- aggregation */

function numeric(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null
  const n =
    typeof value === "number" ? value : Number(String(value).replace(/,/g, ""))
  return Number.isFinite(n) ? n : null
}

/** Which columns hold the place and the measure, and the places themselves. */
function geoColumns(panel: DashboardPanel) {
  const xi = panel.x ? columnIndex(panel.columns, panel.x) : 0
  const yName = Array.isArray(panel.y) ? panel.y[0] : panel.y
  const yi = yName ? columnIndex(panel.columns, yName) : 1
  const placeIdx = xi >= 0 ? xi : 0
  const valueIdx = yi >= 0 ? yi : 1
  return {
    placeIdx,
    valueIdx,
    places: panel.rows.map((row) => row[placeIdx]),
    valueColumn: panel.columns[valueIdx] ?? "",
  }
}

type Totals = {
  byId: Map<string, number>
  lo: number
  hi: number
}

/**
 * One number per shape.
 *
 * Summed rather than last-wins: a query grouped by place AND something else
 * yields several rows per place, and showing only the last one would be a
 * quietly wrong map.
 */
function totalByPlace(
  rows: unknown[][],
  placeIdx: number,
  valueIdx: number,
  idOf: (value: unknown) => string | undefined
): Totals {
  const byId = new Map<string, number>()
  for (const row of rows) {
    const id = idOf(row[placeIdx])
    const value = numeric(row[valueIdx])
    if (!id || value === null) continue
    byId.set(id, (byId.get(id) ?? 0) + value)
  }
  const values = [...byId.values()]
  return {
    byId,
    lo: values.length ? Math.min(...values) : 0,
    hi: values.length ? Math.max(...values) : 0,
  }
}

/* The lightest shade the ramp may start at.

   heatColor(0) is #E8F1FA, and the "no data" fill is var(--muted) — on the
   default theme those are three hex digits apart. The lowest-valued place then
   reads as having no data at all, which on a world map full of grey context
   countries is the difference between "we hold $300m in the UAE" and "we are
   not in the UAE". Starting the ramp part-way up keeps least distinct from
   none. */
const SHADE_FLOOR = 0.18

function shadeScale(lo: number, hi: number): (value: number) => string {
  const span = hi - lo
  return (value: number) =>
    heatColor(
      span === 0 ? 1 : SHADE_FLOOR + (1 - SHADE_FLOOR) * ((value - lo) / span)
    )
}

/* ------------------------------------------------------------------ chrome  */

function GeoLegend({
  panel,
  valueColumn,
  lo,
  hi,
  missed,
  noun,
}: {
  panel: DashboardPanel
  valueColumn: string
  lo: number
  hi: number
  missed: string[]
  noun: string
}) {
  return (
    <div className="mt-1 flex items-center justify-between gap-3 px-1 text-[11px] text-muted-foreground">
      <span className="flex items-center gap-1.5">
        {/* Compact, like an axis: the legend is a scale to read against, and
            "AED 5,100,131.98" is a figure to audit. Hover keeps full precision. */}
        <span>{formatAxis(valueColumn, lo, panel.column_formats, panel.default_currency)}</span>
        <span
          className="h-2 w-16 rounded-sm"
          style={{
            background: `linear-gradient(to right, ${heatColor(SHADE_FLOOR)}, ${heatColor(1)})`,
          }}
        />
        <span>{formatAxis(valueColumn, hi, panel.column_formats, panel.default_currency)}</span>
      </span>
      {missed.length > 0 ? (
        <span
          className="truncate text-amber-600 dark:text-amber-500"
          title={missed.join(", ")}
        >
          {missed.length} value{missed.length === 1 ? "" : "s"} not matched to a{" "}
          {noun}
        </span>
      ) : null}
    </div>
  )
}

/* ------------------------------------------------------------- place labels */

/** The name each place is written under in the data, by shape id. */
function placeNames(
  rows: unknown[][],
  placeIdx: number,
  idOf: (value: unknown) => string | undefined
): Map<string, string> {
  const names = new Map<string, string>()
  for (const row of rows) {
    const raw = row[placeIdx]
    const id = idOf(raw)
    if (id && !names.has(id) && raw !== null && raw !== undefined) {
      names.set(id, String(raw).trim())
    }
  }
  return names
}

type PlacePoint = {
  id: string
  name: string
  value: number
  cx: number
  cy: number
  /** Too small to see at this zoom, so the label stands off the shape. */
  tiny: boolean
}

/**
 * Names and values printed on the map itself.
 *
 * A shaded map says which places are high only if a reader already knows
 * where they are, and names only on hover means nothing about the best and
 * worst performer is visible at a glance. The standard labels those two;
 * a panel can ask for every place instead, or none.
 */
function PlaceLabels({
  points,
  mode,
  fmt,
  fontSize,
}: {
  points: PlacePoint[]
  mode: DataLabels
  fmt: AxisFormat
  /** In the map's own viewBox units, so labels scale with it. */
  fontSize: number
}) {
  if (mode === "none" || points.length === 0) return null
  // One label per place. An atlas can hold several shapes under one id; the
  // largest visible one is the place a reader would look for.
  const byPlace = new Map<string, PlacePoint>()
  for (const point of points) {
    const seen = byPlace.get(point.id)
    if (!seen || (seen.tiny && !point.tiny)) byPlace.set(point.id, point)
  }
  const unique = [...byPlace.values()]
  const chosen = mode === "all"
    ? unique
    : labelIndices(unique.map((p) => p.value), mode).map((i) => unique[i])
  if (chosen.length === 0) return null

  // Two labels close together would print on top of each other — the GCC's
  // smallest states sit side by side — so a later one steps below the earlier.
  const placed: Array<{ point: PlacePoint; x: number; y: number }> = []
  for (const point of chosen) {
    const x = point.cx
    let y = point.tiny ? point.cy - fontSize * 1.6 : point.cy
    for (const other of placed) {
      if (Math.abs(other.x - x) < fontSize * 7 && Math.abs(other.y - y) < fontSize * 2.4) {
        y = other.y + fontSize * 2.6
      }
    }
    placed.push({ point, x, y })
  }

  return (
    <g aria-hidden pointerEvents="none">
      {placed.map(({ point, x, y }) => (
        <g key={point.id}>
          {point.tiny ? (
            <line
              x1={point.cx}
              y1={point.cy}
              x2={x}
              y2={y + fontSize * 0.3}
              stroke="var(--muted-foreground)"
              strokeWidth={fontSize * 0.06}
            />
          ) : null}
          <text
            x={x}
            y={y}
            textAnchor="middle"
            fontSize={fontSize}
            fontWeight={600}
            fill="var(--foreground)"
            stroke="var(--background)"
            strokeWidth={fontSize * 0.28}
            paintOrder="stroke"
            strokeLinejoin="round"
          >
            {point.name}
            <tspan x={x} dy={fontSize * 1.15} fontWeight={500} fontSize={fontSize * 0.9}>
              {formatLabelValue(point.value, fmt)}
            </tspan>
          </text>
        </g>
      ))}
    </g>
  )
}

function MapSkeleton() {
  return (
    <div className="flex h-full w-full items-center justify-center">
      <span className="text-[11px] text-muted-foreground">Loading map…</span>
    </div>
  )
}

/* ------------------------------------------------------------------ US map  */

function UsStatesMap({ panel }: { panel: DashboardPanel }) {
  const atlas = useAtlas(usAtlas, loadUsAtlas)

  /* One memo over the whole panel rather than several over its parts: the
     parts are freshly built objects, so a memo keyed on them recomputes every
     render anyway, and the React Compiler cannot preserve the memoization. */
  const { byId, lo, hi, missed, valueColumn, names } = React.useMemo(() => {
    const columns = geoColumns(panel)
    const idOf = (value: unknown) => matchState(value)?.fips
    return {
      ...totalByPlace(panel.rows, columns.placeIdx, columns.valueIdx, idOf),
      missed: unmatched(columns.places),
      valueColumn: columns.valueColumn,
      names: placeNames(panel.rows, columns.placeIdx, idOf),
    }
  }, [panel])

  const path = React.useMemo(() => geoPath(), [])
  if (!atlas) return <MapSkeleton />

  const shade = shadeScale(lo, hi)

  return (
    <div className="flex h-full w-full flex-col">
      <svg
        viewBox={atlas.viewBox}
        className="h-full w-full"
        role="img"
        aria-label={panel.title ?? "Map"}
      >
        {atlas.features.map((f) => {
          const fips = String(f.id ?? "").padStart(2, "0")
          const value = byId.get(fips)
          const state = stateByFips(fips)
          return (
            <path
              key={fips}
              d={path(f as GeoPermissibleObjects) ?? undefined}
              fill={value === undefined ? "var(--muted)" : shade(value)}
              stroke="var(--background)"
              strokeWidth={0.75}
            >
              {/* Native tooltip: no library, and it survives export to PDF or
                  a screenshot as plain markup. */}
              <title>
                {state?.name ?? fips}
                {value === undefined
                  ? " — no data"
                  : ` — ${formatCell(valueColumn, value, panel.column_formats, null, panel.default_currency)}`}
              </title>
            </path>
          )
        })}
        <PlaceLabels
          mode={dataLabelsFor(panel.chart_type, byId.size, panel.format?.data_labels)}
          fmt={axisFormat({
            name: valueColumn,
            formats: panel.column_formats,
            defaultCurrency: panel.default_currency,
            scale: { domain: [lo, hi], ticks: [lo, hi] },
          })}
          fontSize={15}
          points={atlas.features.flatMap((f) => {
            const fips = String(f.id ?? "").padStart(2, "0")
            const value = byId.get(fips)
            if (value === undefined) return []
            const [cx, cy] = path.centroid(f as GeoPermissibleObjects)
            if (!Number.isFinite(cx) || !Number.isFinite(cy)) return []
            return [{
              id: fips,
              name: names.get(fips) ?? stateByFips(fips)?.name ?? fips,
              value,
              cx,
              cy,
              tiny: path.area(f as GeoPermissibleObjects) < 60,
            }]
          })}
        />
      </svg>
      <GeoLegend
        panel={panel}
        valueColumn={valueColumn}
        lo={lo}
        hi={hi}
        missed={missed}
        noun="state"
      />
    </div>
  )
}

/* --------------------------------------------------------------- world map  */

/** The logical canvas the projection is fitted into. */
const W = 800
const H = 450
const PAD = 6

/* Floors on how far the fit may zoom. One country alone would otherwise fill
   the frame edge to edge with no coastline around it to say where on earth it
   is — roughly the span of western Europe is enough to place anywhere. */
const MIN_LON_SPAN = 24
const MIN_LAT_SPAN = 18

/* Antarctica is in the boundary file and is never the subject. Drawn, it eats
   the bottom fifth of every world view for a landmass nobody has data on. */
const ANTARCTICA = "010"

/**
 * The area the projection should frame: the matched countries, padded — or the
 * whole globe when they are spread too far for framing to mean anything.
 *
 * Returned as a MultiPoint sampling the box's perimeter rather than as the
 * Polygon it obviously is. A Polygon would be read as a SPHERICAL polygon, and
 * d3 takes the interior to be the side the ring winds around: get the winding
 * backwards and the box means "the whole earth except this box", which fits to
 * exactly the world view you were trying to zoom in from. That failure is
 * silent — a correct-looking world map — and cost an hour here. Points have no
 * interior and no winding, so there is nothing to get backwards. They are
 * sampled along the edges, not just at the corners, because in a curved
 * projection the extreme of an edge is usually somewhere in its middle.
 */
function focusArea(matched: PlaceFeature[]): GeoGeometryObjects {
  const sphere = { type: "Sphere" } as unknown as GeoGeometryObjects
  if (matched.length === 0) return sphere

  const [[west, south], [east, north]] = geoBounds({
    type: "FeatureCollection",
    features: matched,
  } as unknown as GeoPermissibleObjects)

  // geoBounds signals a set straddling the antimeridian by returning a west
  // greater than its east. Fitting to that is not a smaller view of anything.
  if (west > east) return sphere

  const lonSpan = east - west
  const latSpan = north - south
  if (lonSpan > 170 || latSpan > 110) return sphere

  const lonPad = Math.max(lonSpan * 0.12, (MIN_LON_SPAN - lonSpan) / 2, 2)
  const latPad = Math.max(latSpan * 0.12, (MIN_LAT_SPAN - latSpan) / 2, 2)
  const w = Math.max(-180, west - lonPad)
  const e = Math.min(180, east + lonPad)
  const s = Math.max(-84, south - latPad)
  const n = Math.min(84, north + latPad)

  const STEPS = 10
  const coordinates: Array<[number, number]> = []
  for (let i = 0; i <= STEPS; i++) {
    const t = i / STEPS
    const lon = w + (e - w) * t
    const lat = s + (n - s) * t
    coordinates.push([lon, s], [lon, n], [w, lat], [e, lat])
  }
  return { type: "MultiPoint", coordinates }
}

/**
 * How this column's values resolve to countries.
 *
 * Normally: whatever `matchCountry` says. But a column holding BOTH US states
 * and countries — "TX", "CA", "Germany" — has to be drawn on the world map,
 * and there "CA" is Canada. California's number would be painted on Canada,
 * silently and confidently wrong; the one thing this panel must never do.
 *
 * So when the data proves it contains real US states (a value that can only be
 * a state, like TX), every value a state table claims is refused a country and
 * reported instead. Mixed geography is the user's to resolve, not ours to
 * guess. A column of bare two-letter codes with no unambiguous state in it —
 * "CA", "IN", "DE" under a COUNTRY heading — is unaffected: nothing there
 * proves US states are in play, so they read as countries.
 */
function countryMatcher(places: unknown[]): (value: unknown) => string | undefined {
  const hasRealStates = geoEvidence(places).onlyState > 0
  return (value: unknown) => {
    if (hasRealStates && matchState(value) !== undefined) return undefined
    return matchCountry(value)?.id
  }
}

/** Values the matcher could not place, so the footer can name them. */
function unplaceable(
  places: unknown[],
  idOf: (value: unknown) => string | undefined
): string[] {
  const missed: string[] = []
  for (const place of places) {
    if (place === null || place === undefined || String(place).trim() === "")
      continue
    if (!idOf(place)) missed.push(String(place))
  }
  return [...new Set(missed)]
}

function CountriesMap({ panel }: { panel: DashboardPanel }) {
  const atlas = useAtlas(worldAtlas, loadWorldAtlas)

  const { byId, lo, hi, missed, valueColumn, names } = React.useMemo(() => {
    const columns = geoColumns(panel)
    const idOf = countryMatcher(columns.places)
    return {
      ...totalByPlace(panel.rows, columns.placeIdx, columns.valueIdx, idOf),
      missed: unplaceable(columns.places, idOf),
      valueColumn: columns.valueColumn,
      names: placeNames(panel.rows, columns.placeIdx, idOf),
    }
  }, [panel])

  const drawn = React.useMemo(
    () =>
      (atlas?.features ?? []).filter(
        (f) => String(f.id) !== ANTARCTICA || byId.has(ANTARCTICA)
      ),
    [atlas, byId]
  )

  const path = React.useMemo(() => {
    const matched = drawn.filter((f) => byId.has(String(f.id)))
    const projection = geoEqualEarth().fitExtent(
      [
        [PAD, PAD],
        [W - PAD, H - PAD],
      ],
      focusArea(matched)
    )
    return geoPath(projection)
  }, [drawn, byId])

  if (!atlas) return <MapSkeleton />

  const shade = shadeScale(lo, hi)

  /* Countries too small to see at this zoom get a dot instead. Singapore, the
     UAE and Bahrain are exactly the places this data is about, and a shape one
     pixel across next to Russia reads as no data at all. */
  const MIN_VISIBLE_AREA = 14

  return (
    <div className="flex h-full w-full flex-col">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-full w-full"
        role="img"
        aria-label={panel.title ?? "Map"}
      >
        {/* Keyed by id AND index: the atlas is not keyed uniquely by ISO
            numeric. Australia and Ashmore and Cartier Is. both carry 036, and
            five shapes (Somaliland, Kosovo, N. Cyprus, Indian Ocean Ter.,
            Siachen Glacier) carry no id at all. */}
        {drawn.map((f, i) => {
          const id = String(f.id ?? "")
          const value = byId.get(id)
          const country = countryById(id)
          return (
            <path
              key={`${id}-${i}`}
              d={path(f as GeoPermissibleObjects) ?? undefined}
              fill={value === undefined ? "var(--muted)" : shade(value)}
              stroke="var(--background)"
              strokeWidth={0.4}
            >
              <title>
                {country?.name ?? f.properties?.name ?? id}
                {value === undefined
                  ? " — no data"
                  : ` — ${formatCell(valueColumn, value, panel.column_formats, null, panel.default_currency)}`}
              </title>
            </path>
          )
        })}

        {drawn.map((f, i) => {
          const id = String(f.id ?? "")
          const value = byId.get(id)
          if (value === undefined) return null
          if (path.area(f as GeoPermissibleObjects) >= MIN_VISIBLE_AREA) return null
          const [cx, cy] = path.centroid(f as GeoPermissibleObjects)
          if (!Number.isFinite(cx) || !Number.isFinite(cy)) return null
          return (
            <circle
              key={`dot-${id}-${i}`}
              cx={cx}
              cy={cy}
              r={4}
              fill={shade(value)}
              stroke="var(--background)"
              strokeWidth={1}
            >
              <title>
                {countryById(id)?.name ?? id} —{" "}
                {formatCell(valueColumn, value, panel.column_formats, null, panel.default_currency)}
              </title>
            </circle>
          )
        })}

        <PlaceLabels
          mode={dataLabelsFor(panel.chart_type, byId.size, panel.format?.data_labels)}
          fmt={axisFormat({
            name: valueColumn,
            formats: panel.column_formats,
            defaultCurrency: panel.default_currency,
            scale: { domain: [lo, hi], ticks: [lo, hi] },
          })}
          fontSize={17}
          points={drawn.flatMap((f) => {
            const id = String(f.id ?? "")
            const value = byId.get(id)
            if (value === undefined) return []
            const [cx, cy] = path.centroid(f as GeoPermissibleObjects)
            if (!Number.isFinite(cx) || !Number.isFinite(cy)) return []
            return [{
              id,
              name: names.get(id) ?? countryById(id)?.name ?? id,
              value,
              cx,
              cy,
              tiny: path.area(f as GeoPermissibleObjects) < MIN_VISIBLE_AREA,
            }]
          })}
        />
      </svg>
      <GeoLegend
        panel={panel}
        valueColumn={valueColumn}
        lo={lo}
        hi={hi}
        missed={missed}
        noun="country"
      />
    </div>
  )
}

/* ------------------------------------------------------------------- panel  */

/**
 * The map panel. Picks its own scope from the data it was given.
 *
 * Both `geo_us_states` and `geo_countries` route here, so a planner that picks
 * the wrong one of the two still renders the right map.
 */
export function GeoPanel({ panel }: { panel: DashboardPanel }) {
  const scope = React.useMemo(
    () =>
      resolveGeoScope(geoColumns(panel).places, {
        chartType: panel.chart_type,
        column: panel.x ?? null,
      }),
    [panel]
  )

  return scope === "countries" ? (
    <CountriesMap panel={panel} />
  ) : (
    <UsStatesMap panel={panel} />
  )
}
