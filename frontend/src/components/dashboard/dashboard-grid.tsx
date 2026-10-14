import * as React from "react"
import { GripVerticalIcon } from "lucide-react"

import { DashboardPanelCard } from "@/components/dashboard/dashboard-panel"
import { PanelErrorBoundary } from "@/components/error-boundary"
import {
  GRID_COLUMNS,
  MAX_ROW_SPAN,
  MIN_CHART_SPAN,
  SPAN_CLASS,
  type LaidOutPanel,
  type PanelRowSpan,
  type PanelSpan,
} from "@/lib/dashboard-layout"
import { cn } from "@/lib/utils"

/** Pixels of drag per height step. */
const ROW_STEP_PX = 150

export type ResizeHandler = (
  panelIndex: number,
  span: PanelSpan,
  rowSpan: PanelRowSpan
) => void

/** Move the panel at `from` to where the panel at `to` currently sits. */
export type ReorderHandler = (from: number, to: number) => void

/**
 * Where a moved panel ends up, mirroring the caller's splice.
 *
 * Removing at `from` and inserting at `to` puts the panel exactly in the slot
 * the target occupied, in both directions — so the landing index IS `to`.
 * Compensating for the removal (inserting at `to - 1` when moving forward) lands
 * it one short: dragging onto the very next panel then changes nothing at all.
 */
function landingIndex(_from: number, to: number): number {
  return to
}

/**
 * A drag handle on a panel's edge.
 *
 * The value at POINTER-DOWN is the baseline every move is measured against.
 * Measuring against the live value instead compounds: the panel re-renders one
 * step wider, the next move adds another step to that already-widened value,
 * and a slow drag rockets to full width — or collapses to one column going the
 * other way, with no way to stop in between.
 *
 * Pointer events rather than mouse events, with setPointerCapture on the way in,
 * so a fast drag that outruns the handle keeps resizing instead of sticking —
 * and so the same code works for touch and pen.
 */
function ResizeHandle({
  orientation,
  label,
  value,
  min,
  max,
  stepSize,
  onChange,
}: {
  orientation: "horizontal" | "vertical"
  label: string
  value: number
  min: number
  max: number
  /** Pixels per step, read at drag time — the grid's width can change. */
  stepSize: () => number
  onChange: (next: number) => void
}) {
  const start = React.useRef<{ pos: number; value: number } | null>(null)
  const axis = (e: React.PointerEvent) =>
    orientation === "horizontal" ? e.clientX : e.clientY

  const commit = (next: number) => {
    const clamped = Math.max(min, Math.min(max, next))
    if (clamped !== value) onChange(clamped)
  }

  return (
    <div
      role="separator"
      aria-label={label}
      aria-orientation={orientation === "horizontal" ? "vertical" : "horizontal"}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      onPointerDown={(e) => {
        e.preventDefault()
        // Capture keeps a fast drag alive once the pointer outruns the handle.
        // It is an enhancement, not the gate: whether a drag is in progress is
        // decided by `start`, so a browser that refuses capture still resizes
        // rather than silently ignoring every move.
        try {
          e.currentTarget.setPointerCapture(e.pointerId)
        } catch {
          /* capture unavailable; the drag still works over the handle */
        }
        start.current = { pos: axis(e), value }
      }}
      onPointerMove={(e) => {
        const from = start.current
        if (!from) return
        // No button held means the drag ended somewhere we never saw the
        // pointerup — a release outside the window, or one swallowed by another
        // element. Without this, `start` stays set and simply MOVING THE MOUSE
        // over the handle keeps resizing.
        if (e.buttons === 0) {
          start.current = null
          return
        }
        const px = stepSize()
        if (!px) return
        commit(from.value + Math.round((axis(e) - from.pos) / px))
      }}
      onPointerUp={(e) => {
        start.current = null
        try {
          e.currentTarget.releasePointerCapture(e.pointerId)
        } catch {
          /* nothing was captured */
        }
      }}
      onPointerCancel={() => {
        start.current = null
      }}
      // Resizing has to be reachable without a mouse, and arrow keys are what a
      // separator answers to.
      onKeyDown={(e) => {
        const back = orientation === "horizontal" ? "ArrowLeft" : "ArrowUp"
        const forward = orientation === "horizontal" ? "ArrowRight" : "ArrowDown"
        if (e.key === back) {
          e.preventDefault()
          commit(value - 1)
        } else if (e.key === forward) {
          e.preventDefault()
          commit(value + 1)
        }
      }}
      className={cn(
        "absolute z-10 flex touch-none items-center justify-center rounded-full",
        "bg-primary/0 hover:bg-primary/20 focus-visible:bg-primary/30",
        "outline-none ring-offset-2 focus-visible:ring-2 focus-visible:ring-ring",
        "transition-colors",
        orientation === "horizontal"
          ? "-right-1.5 top-4 bottom-4 w-3 cursor-col-resize"
          : "-bottom-1.5 left-4 right-4 h-3 cursor-row-resize"
      )}
    >
      <span
        className={cn(
          "rounded-full bg-border",
          orientation === "horizontal" ? "h-10 w-1" : "h-1 w-10"
        )}
      />
    </div>
  )
}

/**
 * The panel grid for one section.
 *
 * Widths are column spans rather than free positions, so panels always flow in
 * order and a drag cannot leave a panel overlapping another or stranded in a
 * gap. `editable` adds the handles; without it this renders exactly what a
 * viewer sees.
 */
export function DashboardGrid({
  panels,
  editable = false,
  onResize,
  onReorder,
  onCategoryClick,
  selectedValue,
}: {
  panels: LaidOutPanel[]
  editable?: boolean
  onResize?: ResizeHandler
  onReorder?: ReorderHandler
  onCategoryClick?: (value: string) => void
  selectedValue?: string | null
}) {
  const gridRef = React.useRef<HTMLDivElement>(null)
  // The panel being dragged, tracked by its CURRENT array index — which changes
  // as it displaces others, so it is re-derived after every move rather than
  // read from the closure.
  const dragging = React.useRef<number | null>(null)
  const [draggingIndex, setDraggingIndex] = React.useState<number | null>(null)

  /** The panel index under a point, from the DOM rather than from geometry we
   *  would otherwise have to keep in step with the grid's own flow. */
  const indexAt = React.useCallback((x: number, y: number): number | null => {
    const el = document
      .elementFromPoint(x, y)
      ?.closest<HTMLElement>("[data-panel-index]")
    if (!el) return null
    const n = Number(el.dataset.panelIndex)
    return Number.isFinite(n) ? n : null
  }, [])

  const endDrag = React.useCallback(() => {
    dragging.current = null
    setDraggingIndex(null)
  }, [])

  // A release that lands outside the grip — outside the window entirely, or on
  // an element that swallows the event — would otherwise leave the drag armed,
  // and the next hover would start rearranging panels again.
  React.useEffect(() => {
    if (draggingIndex === null) return
    window.addEventListener("pointerup", endDrag)
    window.addEventListener("pointercancel", endDrag)
    return () => {
      window.removeEventListener("pointerup", endDrag)
      window.removeEventListener("pointercancel", endDrag)
    }
  }, [draggingIndex, endDrag])

  /** Width of one grid column right now, to turn pixels into spans. */
  const columnWidth = React.useCallback(() => {
    const el = gridRef.current
    if (!el) return 0
    const styles = getComputedStyle(el)
    const gap = parseFloat(styles.columnGap || "0") || 0
    const columns =
      styles.gridTemplateColumns.split(" ").filter(Boolean).length || 1
    return (el.clientWidth - gap * (columns - 1)) / columns
  }, [])

  if (panels.length === 0) return null

  return (
    // Container queries rather than `lg:`: the grid has to reflow on the width
    // it actually has, which the resizable sidebar changes without the viewport
    // moving at all. `min-w-0` on each cell so a wide table inside a panel
    // cannot set the column's floor and pin the grid open.
    <div className="@container/panels">
      <div
        ref={gridRef}
        className="grid gap-4 @xl/panels:grid-cols-2 @3xl/panels:grid-cols-4"
      >
        {panels.map((item, position) => (
          <div
            // Keyed by TITLE, not array position: position changes on every
            // reorder, and an index key makes React tear down and rebuild each
            // moved panel — charts remount and flicker, and a drag in progress
            // loses the node under the pointer. Title is the identity the
            // layout save already matches on.
            key={item.panel.title}
            data-panel-index={item.index}
            /* The export finds a panel's rendered SVG by title, to send a
               picture of the shapes the PDF cannot draw itself. */
            data-panel-title={item.panel.title}
            className={cn(
              "relative min-w-0",
              SPAN_CLASS[item.span],
              editable && "rounded-lg ring-1 ring-dashed ring-border",
              draggingIndex === item.index && "opacity-60 ring-2 ring-primary"
            )}
          >
            <div className="h-full">
              <PanelErrorBoundary title={item.panel.title} resetKey={item.panel}>
                <DashboardPanelCard
                  panel={item.panel}
                  onCategoryClick={editable ? undefined : onCategoryClick}
                  selectedValue={editable ? null : selectedValue}
                />
              </PanelErrorBoundary>
            </div>

            {editable ? (
              <>
                {/* Grip rather than the whole card: a panel holds a table you
                    may want to scroll and text you may want to select, so
                    dragging is deliberate and starts from one spot. */}
                <div
                  role="button"
                  tabIndex={0}
                  aria-label={`Reorder ${item.panel.title}`}
                  title="Drag to reorder"
                  onPointerDown={(e) => {
                    e.preventDefault()
                    try {
                      e.currentTarget.setPointerCapture(e.pointerId)
                    } catch {
                      /* capture unavailable; dragging still works */
                    }
                    dragging.current = item.index
                    setDraggingIndex(item.index)
                  }}
                  onPointerMove={(e) => {
                    const from = dragging.current
                    if (from === null) return
                    // Same stuck-drag guard as the resize handles, and the
                    // reason a plain hover was rearranging the dashboard: with
                    // no button held there is no drag, whatever `dragging` says.
                    if (e.buttons === 0) {
                      endDrag()
                      return
                    }
                    const to = indexAt(e.clientX, e.clientY)
                    if (to === null || to === from) return
                    onReorder?.(from, to)
                    dragging.current = landingIndex(from, to)
                    setDraggingIndex(dragging.current)
                  }}
                  onPointerUp={(e) => {
                    endDrag()
                    try {
                      e.currentTarget.releasePointerCapture(e.pointerId)
                    } catch {
                      /* nothing was captured */
                    }
                  }}
                  onPointerCancel={endDrag}
                  onKeyDown={(e) => {
                    const back = e.key === "ArrowLeft" || e.key === "ArrowUp"
                    const fwd = e.key === "ArrowRight" || e.key === "ArrowDown"
                    if (!back && !fwd) return
                    const neighbour = panels[position + (back ? -1 : 1)]
                    if (!neighbour) return
                    e.preventDefault()
                    onReorder?.(item.index, neighbour.index)
                  }}
                  className={cn(
                    "absolute left-1.5 top-1.5 z-20 flex size-6 cursor-grab touch-none",
                    "items-center justify-center rounded-md bg-background/80 text-muted-foreground",
                    "opacity-60 shadow-sm outline-none transition hover:opacity-100",
                    "focus-visible:ring-2 focus-visible:ring-ring active:cursor-grabbing"
                  )}
                >
                  <GripVerticalIcon className="size-4" />
                </div>

                <ResizeHandle
                  orientation="horizontal"
                  label={`Width of ${item.panel.title}`}
                  value={item.span}
                  min={MIN_CHART_SPAN}
                  max={GRID_COLUMNS}
                  stepSize={columnWidth}
                  onChange={(next) =>
                    onResize?.(item.index, next as PanelSpan, item.rowSpan)
                  }
                />
                <ResizeHandle
                  orientation="vertical"
                  label={`Height of ${item.panel.title}`}
                  value={item.rowSpan}
                  min={1}
                  max={MAX_ROW_SPAN}
                  stepSize={() => ROW_STEP_PX}
                  onChange={(next) =>
                    onResize?.(item.index, item.span, next as PanelRowSpan)
                  }
                />
              </>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  )
}
