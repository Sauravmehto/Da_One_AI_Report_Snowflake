import { AlertTriangleIcon, Loader2Icon } from "lucide-react"

import { SuccessCheck } from "@/components/admin/success-check"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { Coverage } from "@/lib/semantic-api"

export type PublishPhase = "confirm" | "publishing" | "done"

/**
 * Publish confirmation.
 *
 * Publishing is the moment a layer becomes what the model believes about a
 * database, so it gets a deliberate stop rather than a one-click action —
 * and the dialog restates what is about to change, because "are you sure?"
 * with no detail trains people to click through it.
 *
 * The success state is held for a beat on purpose. Publishing is otherwise
 * invisible: the page looks identical before and after, and a silent success
 * leaves you wondering whether it worked.
 */
export function PublishDialog({
  open,
  phase,
  database,
  coverage,
  warningCount,
  isFirstPublish,
  onConfirm,
  onOpenChange,
}: {
  open: boolean
  phase: PublishPhase
  database: string
  coverage: Coverage | null
  warningCount: number
  isFirstPublish: boolean
  onConfirm: () => void
  onOpenChange: (open: boolean) => void
}) {
  const busy = phase === "publishing"

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Not dismissable mid-flight: closing would leave the user unsure
        // whether the publish landed.
        if (busy) return
        onOpenChange(next)
      }}
    >
      <DialogContent showCloseButton={!busy && phase !== "done"}>
        {phase === "done" ? (
          <SuccessPanel database={database} isFirstPublish={isFirstPublish} />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Publish this semantic layer?</DialogTitle>
              <DialogDescription>
                {isFirstPublish ? (
                  <>
                    <span className="font-medium text-foreground">
                      {database}
                    </span>{" "}
                    becomes available in chat, and the model starts using these
                    definitions to write SQL.
                  </>
                ) : (
                  <>
                    This replaces the live layer for{" "}
                    <span className="font-medium text-foreground">
                      {database}
                    </span>
                    . Every new question about this database uses the updated
                    definitions.
                  </>
                )}
              </DialogDescription>
            </DialogHeader>

            {coverage ? (
              <dl className="grid grid-cols-3 gap-3 rounded-lg border bg-muted/40 p-3 text-center">
                <Stat
                  label="Tables"
                  value={`${coverage.documented_tables}/${coverage.total_tables}`}
                />
                <Stat
                  label="Columns"
                  value={`${coverage.documented_columns}/${coverage.total_columns}`}
                />
                <Stat
                  label="Date roles"
                  value={String(coverage.tables_with_date_roles)}
                />
              </dl>
            ) : null}

            {warningCount > 0 ? (
              <p className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400">
                <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" />
                <span>
                  {warningCount} warning{warningCount === 1 ? "" : "s"} — mostly
                  undescribed columns. These do not block publishing; the model
                  simply sees less.
                </span>
              </p>
            ) : null}

            <DialogFooter>
              <Button
                variant="ghost"
                onClick={() => onOpenChange(false)}
                disabled={busy}
              >
                Keep editing
              </Button>
              <Button onClick={onConfirm} disabled={busy}>
                {busy ? (
                  <>
                    <Loader2Icon className="size-4 animate-spin" />
                    Publishing…
                  </>
                ) : (
                  "Publish"
                )}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] tracking-wide text-muted-foreground uppercase">
        {label}
      </dt>
      <dd className="mt-0.5 font-medium tabular-nums">{value}</dd>
    </div>
  )
}

function SuccessPanel({
  database,
  isFirstPublish,
}: {
  database: string
  isFirstPublish: boolean
}) {
  return (
    <div className="flex flex-col items-center gap-4 py-6 text-center">
      <SuccessCheck />

      <div className="animate-publish-text space-y-1">
        <DialogTitle className="text-base">Published</DialogTitle>
        <DialogDescription>
          {isFirstPublish ? (
            <>
              <span className="font-medium text-foreground">{database}</span> is
              now available in chat.
            </>
          ) : (
            <>
              The live layer for{" "}
              <span className="font-medium text-foreground">{database}</span> is
              updated. No restart needed.
            </>
          )}
        </DialogDescription>
      </div>
    </div>
  )
}
