/**
 * One accent colour per section of the app.
 *
 * Stated here rather than picked per component, because the value of the
 * colour is that the same thing is the same colour everywhere: a green glyph
 * in the sidebar, on the page heading and beside a row all have to mean
 * "report" or the colour is decoration rather than information.
 *
 * Deliberately few, and applied ONLY to a section's own icon. Colouring
 * buttons, borders or text as well turns a calm interface into a busy one, and
 * the accent stops being a landmark once everything has one.
 *
 * Chosen at the 500/600 end so each holds up against both a white and a dark
 * background without a per-theme override.
 */
export const ACCENT = {
  /** Chat and exploration — the primary path through the app. */
  explore: "text-sky-500",
  /** Saved dashboards. */
  dashboards: "text-violet-500",
  /** Generated Excel reports. Green, matching the spreadsheets they produce. */
  reports: "text-emerald-600 dark:text-emerald-500",
  /** Uploaded folders. Matches the folder glyph in the files library. */
  files: "text-amber-500",
  /** The semantic layer, which is configuration rather than content. */
  semantic: "text-teal-500",
} as const

export type AccentName = keyof typeof ACCENT

/**
 * Colour, no fill. The default, because lucide is an OUTLINE set: 24-unit grid,
 * 2-unit stroke. At 16px that stroke is most of a small shape's width, so
 * flooding the glyph and carving the stroke back out in the page colour leaves
 * almost nothing legible — `layout-dashboard`'s 7x5 panels come back as 5x3
 * smudges. Colour alone is what makes these read as sections; filling them
 * makes them read as nothing.
 */
export const OUTLINE = {
  fill: "none",
  strokeWidth: 2,
} as const

/**
 * A solid silhouette, for a glyph that is ONE simple closed shape.
 *
 * A folder survives this and gains from it — it is the container the files sit
 * in, and a solid block of amber says so at a glance. Anything with interior
 * detail does not survive it.
 */
export const SOLID = {
  fill: "currentColor",
  stroke: "currentColor",
  strokeWidth: 2,
} as const

/** A section's icon in its accent colour.
 *
 *  `variant` is "outline" for anything with interior detail, "solid" only for a
 *  single closed shape. */
export function accent(
  name: AccentName,
  size = "size-4",
  variant: "outline" | "solid" = "outline"
) {
  return {
    className: `${size} shrink-0 ${ACCENT[name]}`,
    ...(variant === "solid" ? SOLID : OUTLINE),
  } as const
}
