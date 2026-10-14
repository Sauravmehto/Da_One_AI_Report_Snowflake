import * as React from "react"

import { useSidebar } from "@/components/ui/sidebar"

export const SIDEBAR_WIDTH_DEFAULT = 288
export const SIDEBAR_WIDTH_MIN = 200
export const SIDEBAR_WIDTH_MAX = 480

const STORAGE_KEY = "sidebar-width"
const KEYBOARD_STEP = 16

function clamp(px: number): number {
  return Math.min(SIDEBAR_WIDTH_MAX, Math.max(SIDEBAR_WIDTH_MIN, px))
}

/**
 * Sidebar width, persisted across sessions.
 *
 * Conversation titles are the reason this is worth having: how much of a title
 * is readable before it truncates is a per-user preference, and the collapse
 * toggle alone only offers all-or-nothing.
 */
export function useSidebarWidth() {
  const [width, setWidth] = React.useState<number>(() => {
    const stored = Number(localStorage.getItem(STORAGE_KEY))
    return Number.isFinite(stored) && stored > 0
      ? clamp(stored)
      : SIDEBAR_WIDTH_DEFAULT
  })

  const set = React.useCallback((px: number) => {
    const next = clamp(px)
    setWidth(next)
    localStorage.setItem(STORAGE_KEY, String(next))
  }, [])

  return [width, set] as const
}

/**
 * Drag handle sitting on the sidebar's right edge.
 *
 * Fixed-positioned at the current width rather than nested inside the sidebar:
 * the sidebar itself is `fixed` at left 0, so this lines up exactly while
 * staying out of the shadcn component we did not write.
 */
export function SidebarResizer({
  width,
  onWidthChange,
}: {
  width: number
  onWidthChange: (px: number) => void
}) {
  const { state, isMobile } = useSidebar()
  const [isDragging, setIsDragging] = React.useState(false)

  // The drag is tracked on the window, not on the handle: a fast pointer
  // outruns a 6px-wide element, and releasing outside it must still end the
  // drag rather than leave the sidebar stuck to the cursor.
  React.useEffect(() => {
    if (!isDragging) return

    // Width transitions are for the collapse animation; during a drag they
    // turn every pointer move into a 200ms lag behind the cursor.
    document.body.dataset.sidebarResizing = "true"

    // The sidebar starts at viewport x=0, so the cursor position IS the width.
    const onMove = (event: PointerEvent) => onWidthChange(event.clientX)
    const onUp = () => setIsDragging(false)

    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onUp)
    window.addEventListener("pointercancel", onUp)
    return () => {
      delete document.body.dataset.sidebarResizing
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onUp)
      window.removeEventListener("pointercancel", onUp)
    }
  }, [isDragging, onWidthChange])

  // No handle when there is no visible edge to drag: collapsed, or on mobile
  // where the sidebar is an overlay sheet.
  if (isMobile || state === "collapsed") return null

  function handlePointerDown(event: React.PointerEvent<HTMLDivElement>) {
    event.preventDefault()
    setIsDragging(true)
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowLeft") {
      event.preventDefault()
      onWidthChange(width - KEYBOARD_STEP)
    } else if (event.key === "ArrowRight") {
      event.preventDefault()
      onWidthChange(width + KEYBOARD_STEP)
    }
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      aria-valuenow={width}
      aria-valuemin={SIDEBAR_WIDTH_MIN}
      aria-valuemax={SIDEBAR_WIDTH_MAX}
      tabIndex={0}
      onPointerDown={handlePointerDown}
      onDoubleClick={() => onWidthChange(SIDEBAR_WIDTH_DEFAULT)}
      onKeyDown={handleKeyDown}
      title="Drag to resize · double-click to reset"
      style={{ left: width - 3 }}
      className="fixed inset-y-0 z-20 hidden w-1.5 cursor-col-resize touch-none focus:outline-none md:block"
    >
      {/* The hit area is deliberately wider than the line it draws. */}
      <div
        data-active={isDragging || undefined}
        className="mx-auto h-full w-px bg-transparent transition-colors hover:bg-sidebar-border data-active:bg-primary"
      />
    </div>
  )
}
