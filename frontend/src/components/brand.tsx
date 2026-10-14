/**
 * Product mark for AI Reporting.
 *
 * One SVG, two sizes. The sidebar and favicon use the mark alone; the login
 * lockup adds the wordmark. Colour follows currentColor so the same drawing
 * works on the light form and the dark brand panel.
 */

import { cn } from "@/lib/utils"

export function BrandMark({
  className,
  alt = "",
}: {
  className?: string
  alt?: string
}) {
  const labelled = alt !== ""
  return (
    <svg
      viewBox="0 0 32 32"
      role={labelled ? "img" : "presentation"}
      aria-label={labelled ? alt : undefined}
      aria-hidden={labelled ? undefined : true}
      className={cn("size-6 shrink-0", className)}
    >
      <rect width="32" height="32" rx="8" className="fill-indigo-600" />
      <rect x="7" y="17" width="4" height="8" rx="1" fill="white" />
      <rect x="14" y="12" width="4" height="13" rx="1" fill="white" opacity="0.92" />
      <rect x="21" y="7" width="4" height="18" rx="1" fill="white" />
    </svg>
  )
}

/**
 * Mark and wordmark.
 *
 * `full` adds the short tagline and needs a wide panel. `compact` is the
 * form that still reads in a phone header.
 */
export function BrandLockup({
  variant = "compact",
  tone = "auto",
  className,
}: {
  variant?: "full" | "compact"
  tone?: "ink" | "inverse" | "auto"
  className?: string
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-3 text-foreground",
        tone === "inverse" && "text-white",
        tone === "ink" && "text-neutral-950",
        className
      )}
    >
      <BrandMark className="size-9" alt="AI Reporting" />
      <span className="min-w-0 leading-tight">
        <span className="block text-lg font-semibold tracking-tight">
          AI Reporting
        </span>
        {variant === "full" ? (
          <span className="mt-0.5 block text-[11px] tracking-[0.16em] text-current/70 uppercase">
            Ask your data
          </span>
        ) : null}
      </span>
    </div>
  )
}
