/**
 * The rotating dashboard screenshots on the login page.
 *
 * ADDING OR CHANGING SLIDES IS A FILE OPERATION, NOT A CODE CHANGE.
 * Drop images into `src/assets/showcase/` and they appear here. Order is the
 * filename order, so prefix them: `01-revenue.png`, `02-fraud-risk.png`. The
 * caption is derived from the name — leading digits and dashes are stripped and
 * the rest becomes words, so `02-fraud-risk.png` captions as "Fraud risk".
 *
 * With the folder empty the component renders nothing at all rather than an
 * empty frame, so the page still looks finished before any images exist.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"

const ROTATE_MS = 5000

const files = import.meta.glob<string>(
  "../assets/showcase/*.{png,jpg,jpeg,webp,avif}",
  { eager: true, import: "default" }
)

export type Slide = { src: string; caption: string }

/** `../assets/showcase/02-fraud-risk.png` → "Fraud risk" */
function captionFor(path: string): string {
  const base = path.split("/").pop()!.replace(/\.[^.]+$/, "")
  const words = base.replace(/^[\d]+[-_]?/, "").replace(/[-_]+/g, " ").trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

const SLIDES: Slide[] = Object.keys(files)
  .sort()
  .map((path) => ({ src: files[path], caption: captionFor(path) }))

export function ShowcaseCarousel({ className }: { className?: string }) {
  const [index, setIndex] = useState(0)
  const [paused, setPaused] = useState(false)

  /* Someone who has asked for less motion gets the first slide and no
     rotation — an auto-advancing panel is exactly what that setting means. */
  const reducedMotion =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches

  const count = SLIDES.length
  const advance = useCallback(() => setIndex((i) => (i + 1) % count), [count])

  /* Restarts on every manual pick, so a slide you chose gets a full turn
     rather than the remainder of the previous one. */
  const timer = useRef<number | null>(null)
  useEffect(() => {
    if (count < 2 || paused || reducedMotion) return
    timer.current = window.setTimeout(advance, ROTATE_MS)
    return () => {
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [index, paused, count, reducedMotion, advance])

  if (count === 0) return null

  return (
    <div
      className={cn("select-none", className)}
      aria-roledescription="carousel"
      aria-label="Dashboards built in AI Reporting"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocusCapture={() => setPaused(true)}
      onBlurCapture={() => setPaused(false)}
    >
      {/* A window frame, so a screenshot reads as a product shot rather than
          as a picture that failed to fill its box. */}
      <div className="relative overflow-hidden rounded-xl border border-white/10 bg-white/[0.04] shadow-2xl shadow-black/40 ring-1 ring-black/20">
        <div className="flex items-center gap-1.5 border-b border-white/10 px-3.5 py-2.5">
          <span className="size-2 rounded-full bg-white/20" />
          <span className="size-2 rounded-full bg-white/20" />
          <span className="size-2 rounded-full bg-white/20" />
        </div>

        {/* Fixed aspect box: slides are stacked and cross-faded, so the panel
            never jumps when images differ in size. */}
        <div className="relative aspect-[16/10]">
          {SLIDES.map((slide, i) => (
            <img
              key={slide.src}
              src={slide.src}
              alt={slide.caption}
              loading={i === 0 ? "eager" : "lazy"}
              aria-hidden={i !== index}
              className={cn(
                "absolute inset-0 size-full object-cover object-top transition-opacity duration-700",
                i === index ? "opacity-100" : "opacity-0"
              )}
            />
          ))}
        </div>
      </div>

      {count > 1 ? (
        <div className="mt-4 flex items-center gap-3">
          {/* Swapping the caption instantly leaves it naming the slide that is
              still fading out; cross-fading it makes two labels overlap and
              read as ghosting. So the old one leaves before the new arrives. */}
          <div className="relative h-4 min-w-0 flex-1">
            {SLIDES.map((slide, i) => (
              <p
                key={slide.src}
                aria-hidden={i !== index}
                className={cn(
                  "absolute inset-0 truncate text-xs text-white/50 transition-opacity duration-300",
                  i === index ? "opacity-100 delay-300" : "opacity-0"
                )}
              >
                {slide.caption}
              </p>
            ))}
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {SLIDES.map((slide, i) => (
              <button
                key={slide.src}
                type="button"
                onClick={() => setIndex(i)}
                aria-label={`Show ${slide.caption}`}
                aria-current={i === index}
                className={cn(
                  "h-1.5 rounded-full transition-all",
                  i === index
                    ? "w-5 bg-white/70"
                    : "w-1.5 bg-white/25 hover:bg-white/45"
                )}
              />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  )
}
