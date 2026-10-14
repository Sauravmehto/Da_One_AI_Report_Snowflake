import * as React from "react"

import { cn } from "@/lib/utils"

/** Shortest the dock may be dragged: header, a line of chat and the composer. */
const MIN_HEIGHT = 220
/** Room always left above the dock, so the form it edits stays usable. */
const MIN_ABOVE = 160
/** How far one arrow-key press moves the edge. */
const KEY_STEP = 24

function readHeight(storageKey: string): number | null {
  try {
    const saved = Number(localStorage.getItem(storageKey))
    return Number.isFinite(saved) && saved > 0 ? saved : null
  } catch {
    return null
  }
}

function saveHeight(storageKey: string, height: number | null) {
  try {
    if (height === null) localStorage.removeItem(storageKey)
    else localStorage.setItem(storageKey, String(height))
  } catch {
    // Storage can be blocked (a private window); the height just is not kept.
  }
}

/**
 * A panel docked at the bottom of a column, whose top edge drags up and down.
 *
 * The vertical twin of ResizableAside. Until someone drags it, it keeps
 * `defaultHeightClassName`. A dragged height is remembered in this browser; the
 * edge answers the arrow keys, and a double-click puts it back to the default.
 * The limits come from the column it sits in: it never shrinks below what its
 * content needs, and never grows over the content above it.
 */
export function ResizableDock({
  storageKey,
  defaultHeightClassName,
  hidden = false,
  className,
  children,
}: {
  storageKey: string
  /** The height before anyone drags, e.g. "h-[48%]". */
  defaultHeightClassName: string
  hidden?: boolean
  className?: string
  children: React.ReactNode
}) {
  const dockRef = React.useRef<HTMLDivElement>(null)
  const [height, setHeight] = React.useState<number | null>(() => readHeight(storageKey))
  const [dragging, setDragging] = React.useState(false)
  const drag = React.useRef<{ startY: number; startHeight: number } | null>(null)
  const dragged = React.useRef<number | null>(null)

  const clamp = React.useCallback((value: number) => {
    const column = dockRef.current?.parentElement?.getBoundingClientRect().height
    const max = column ? Math.max(MIN_HEIGHT, column - MIN_ABOVE) : value
    return Math.round(Math.min(max, Math.max(MIN_HEIGHT, value)))
  }, [])

  // A saved height from a taller window must not push the form off screen.
  React.useEffect(() => {
    if (height === null || hidden) return
    const fit = () => setHeight((current) => (current === null ? null : clamp(current)))
    fit()
    window.addEventListener("resize", fit)
    return () => window.removeEventListener("resize", fit)
  }, [height, hidden, clamp])

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const current = dockRef.current?.getBoundingClientRect().height ?? MIN_HEIGHT
    drag.current = { startY: event.clientY, startHeight: current }
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      // No capturable pointer (a synthetic event); the drag still works over the handle.
    }
    setDragging(true)
    event.preventDefault()
  }

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    // The edge is on top, so moving the pointer up makes the dock taller.
    const { startY, startHeight } = drag.current
    const next = clamp(startHeight + (startY - event.clientY))
    dragged.current = next
    setHeight(next)
  }

  const endDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    drag.current = null
    setDragging(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    if (dragged.current !== null) saveHeight(storageKey, dragged.current)
    dragged.current = null
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return
    event.preventDefault()
    const current = height ?? dockRef.current?.getBoundingClientRect().height ?? MIN_HEIGHT
    const next = clamp(current + (event.key === "ArrowUp" ? KEY_STEP : -KEY_STEP))
    setHeight(next)
    saveHeight(storageKey, next)
  }

  const reset = () => {
    setHeight(null)
    saveHeight(storageKey, null)
  }

  return (
    <div
      ref={dockRef}
      hidden={hidden}
      className={cn(
        "relative flex shrink-0 flex-col",
        height === null && defaultHeightClassName,
        className
      )}
      style={height === null ? undefined : { height }}
    >
      <div
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize the assistant"
        aria-valuemin={MIN_HEIGHT}
        aria-valuenow={height ?? undefined}
        tabIndex={0}
        title="Drag to resize · double-click to reset"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={reset}
        onKeyDown={onKeyDown}
        className="group/dock flex h-4 shrink-0 cursor-row-resize touch-none items-center justify-center outline-none"
      >
        {/* A grip that thickens while hovered, focused or dragged. */}
        <span
          aria-hidden
          className={cn(
            "h-1 w-10 rounded-full bg-border transition-all",
            "group-hover/dock:w-14 group-hover/dock:bg-violet-500/50",
            "group-focus-visible/dock:w-14 group-focus-visible/dock:bg-violet-500/70",
            dragging && "w-14 bg-violet-500/70"
          )}
        />
      </div>
      {dragging ? <div className="fixed inset-0 z-10 cursor-row-resize select-none" /> : null}
      {children}
    </div>
  )
}
