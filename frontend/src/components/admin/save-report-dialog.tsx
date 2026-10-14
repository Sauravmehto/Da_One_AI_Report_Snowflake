import { Loader2Icon } from "lucide-react"

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

export type SavePhase = "confirm" | "saving" | "done"

/**
 * Confirm saving a configured workbook as a report.
 *
 * Same shape as PublishDialog, for the same reason: saving is otherwise
 * invisible — the builder looks identical before and after — so a silent
 * success leaves you unsure whether anything was kept. The success state is
 * held for a beat and names where the report went, because the next thing you
 * want to know is where to find it.
 *
 * The confirm step restates what is about to be stored. Two things are being
 * committed at once — the workbook itself and the queries bound to it — and
 * that is worth a sentence, since only one of them is visible on this screen.
 */
export function SaveReportDialog({
  open,
  phase,
  reportName,
  areaCount,
  parameterName,
  onConfirm,
  onOpenChange,
}: {
  open: boolean
  phase: SavePhase
  reportName: string
  areaCount: number
  parameterName?: string | null
  onConfirm: () => void
  onOpenChange: (open: boolean) => void
}) {
  const busy = phase === "saving"

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Not dismissable mid-flight: closing would leave the user unsure
        // whether the save landed.
        if (busy) return
        onOpenChange(next)
      }}
    >
      <DialogContent showCloseButton={!busy && phase !== "done"}>
        {phase === "done" ? (
          <div className="flex flex-col items-center gap-4 py-6 text-center">
            <SuccessCheck />
            <div className="animate-publish-text space-y-1">
              <DialogTitle className="text-base">Report saved</DialogTitle>
              <DialogDescription>
                <span className="font-medium text-foreground">{reportName}</span>{" "}
                is in the Reports section. Open it there to generate a fresh copy
                for another date.
              </DialogDescription>
            </div>
          </div>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Save this as a report?</DialogTitle>
              <DialogDescription>
                Keeps the workbook and the {areaCount}{" "}
                {areaCount === 1 ? "query" : "queries"} you bound to it, so{" "}
                <span className="font-medium text-foreground">{reportName}</span>{" "}
                can be regenerated later without configuring it again.
              </DialogDescription>
            </DialogHeader>

            <ul className="space-y-1.5 rounded-lg border bg-muted/40 p-3 text-xs text-muted-foreground">
              <li>
                The template is stored as uploaded — every run starts from it, so
                the formulas and layout never drift.
              </li>
              <li>
                {parameterName
                  ? `Whoever runs it picks the ${parameterName}; the queries are otherwise fixed.`
                  : "The queries run exactly as written — this report takes no date."}
              </li>
            </ul>

            <DialogFooter>
              <Button
                variant="ghost"
                onClick={() => onOpenChange(false)}
                disabled={busy}
              >
                Keep configuring
              </Button>
              <Button onClick={onConfirm} disabled={busy}>
                {busy ? (
                  <>
                    <Loader2Icon className="size-4 animate-spin" />
                    Saving…
                  </>
                ) : (
                  "Save report"
                )}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
