import * as React from "react"

import { cn } from "@/lib/utils"

/** Narrowest the panel may be dragged: the chat composer needs this much. */
const MIN_WIDTH = 340
/** Room always left for the dashboard beside the panel, so it stays readable. */
const MIN_DASHBOARD_WIDTH = 480
/** Hard ceiling for very wide screens. */
const MAX_WIDTH = 960
/** How far one arrow-key press moves the edge. */
const KEY_STEP = 24

/**
 * The widest the panel may be, from the space its row actually has.
 *
 * Measured from the row, not the window: the app's own sidebar takes a share of
 * the window, and a limit taken from the window let the panel squeeze the
 * dashboard beside it to under 200px.
 */
function maxWidth(row: Element | null | undefined): number {
  const room =
    row?.getBoundingClientRect().width ??
    (typeof window === "undefined" ? MAX_WIDTH : window.innerWidth)
  return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, room - MIN_DASHBOARD_WIDTH))
}

const clampTo = (width: number, row: Element | null | undefined) =>
  Math.round(Math.min(maxWidth(row), Math.max(MIN_WIDTH, width)))

function readWidth(storageKey: string): number | null {
  try {
    const saved = Number(localStorage.getItem(storageKey))
    return Number.isFinite(saved) && saved > 0 ? saved : null
  } catch {
    return null
  }
}

function saveWidth(storageKey: string, width: number | null) {
  try {
    if (width === null) localStorage.removeItem(storageKey)
    else localStorage.setItem(storageKey, String(width))
  } catch {
    // Storage can be blocked (a private window); the width just is not kept.
  }
}

/**
 * A right-hand side panel whose left edge can be dragged to make it wider.
 *
 * Until someone drags it the panel keeps `defaultWidthClassName`, so nothing
 * changes for a reader who never touches the edge. A dragged width is
 * remembered in this browser only. The edge also answers the keyboard — arrow
 * keys move it — and a double-click puts the panel back to its default width.
 * Below the md breakpoint the panel takes the whole screen, so there is no
 * edge to drag.
 */
export function ResizableAside({
  storageKey,
  defaultWidthClassName,
  className,
  children,
}: {
  storageKey: string
  /** The md+ width before anyone drags, e.g. "md:w-[380px] lg:w-[420px]". */
  defaultWidthClassName: string
  className?: string
  children: React.ReactNode
}) {
  const asideRef = React.useRef<HTMLElement>(null)
  const [width, setWidth] = React.useState<number | null>(() =>
    readWidth(storageKey)
  )
  const [dragging, setDragging] = React.useState(false)
  const drag = React.useRef<{ startX: number; startWidth: number } | null>(
    null
  )
  const clamp = (value: number) =>
    clampTo(value, asideRef.current?.parentElement)

  // A saved width from a wider screen, or a window made narrower, must not
  // leave the panel wider than the row now allows.
  React.useEffect(() => {
    if (width === null) return
    const fit = () =>
      setWidth((current) =>
        current === null
          ? null
          : clampTo(current, asideRef.current?.parentElement)
      )
    fit()
    window.addEventListener("resize", fit)
    return () => window.removeEventListener("resize", fit)
  }, [width])

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const current = asideRef.current?.getBoundingClientRect().width ?? MIN_WIDTH
    drag.current = { startX: event.clientX, startWidth: current }
    try {
      // Keeps the drag alive when the pointer outruns the thin handle.
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      // No capturable pointer (a synthetic event); the drag still works while
      // the pointer stays over the handle and the sheet below it.
    }
    setDragging(true)
    event.preventDefault()
  }

  // The width the drag has reached, kept outside state so the end of the drag
  // can save it without reading state mid-update.
  const dragged = React.useRef<number | null>(null)

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    // The edge is on the left, so moving the pointer left widens the panel.
    const { startX, startWidth } = drag.current
    const next = clamp(startWidth + (startX - event.clientX))
    dragged.current = next
    setWidth(next)
  }

  const endDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    drag.current = null
    setDragging(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    // A press with no movement is not a resize: leave the default alone.
    if (dragged.current !== null) saveWidth(storageKey, dragged.current)
    dragged.current = null
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return
    event.preventDefault()
    const current =
      width ?? asideRef.current?.getBoundingClientRect().width ?? MIN_WIDTH
    const next = clamp(
      current + (event.key === "ArrowLeft" ? KEY_STEP : -KEY_STEP)
    )
    setWidth(next)
    saveWidth(storageKey, next)
  }

  const reset = () => {
    setWidth(null)
    saveWidth(storageKey, null)
  }

  return (
    <aside
      ref={asideRef}
      className={cn(
        "relative flex w-full shrink-0 flex-col",
        width === null ? defaultWidthClassName : "md:w-(--aside-width)",
        className
      )}
      style={
        width === null
          ? undefined
          : ({ "--aside-width": `${width}px` } as React.CSSProperties)
      }
    >
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize panel"
        aria-valuemin={MIN_WIDTH}
        aria-valuemax={MAX_WIDTH}
        aria-valuenow={width ?? undefined}
        tabIndex={0}
        title="Drag to resize · double-click to reset"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={reset}
        onKeyDown={onKeyDown}
        className="group/resize absolute inset-y-0 -left-1.5 z-20 hidden w-3 cursor-col-resize touch-none outline-none md:block"
      >
        {/* The visible edge: a hairline that thickens while hovered, focused
            or dragged, so the handle is findable without cluttering the seam. */}
        <span
          aria-hidden
          className={cn(
            "absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-border transition-all",
            "group-hover/resize:w-1 group-hover/resize:bg-primary/40",
            "group-focus-visible/resize:w-1 group-focus-visible/resize:bg-primary/60",
            dragging && "w-1 bg-primary/60"
          )}
        />
      </div>
      {/* While dragging, a transparent sheet over the page keeps the pointer
          from selecting text or waking hover states in the charts beneath. */}
      {dragging ? (
        <div className="fixed inset-0 z-10 cursor-col-resize select-none" />
      ) : null}
      {children}
    </aside>
  )
}
