import { Loader2Icon, Trash2Icon } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

/**
 * Confirm removing something other people can also see.
 *
 * Names what is going and notes that it goes for everyone, since a saved
 * dashboard or report is shared. No type-the-name-to-confirm ritual: the row is
 * deactivated rather than dropped, so friction should match the stakes.
 */
export function DeleteDialog({
  open,
  name,
  kind,
  busy,
  error,
  onConfirm,
  onOpenChange,
}: {
  open: boolean
  name: string
  kind: "dashboard" | "report" | "spreadsheet" | "file" | "folder"
  busy: boolean
  error?: string | null
  onConfirm: () => void
  onOpenChange: (open: boolean) => void
}) {
  // A dashboard or report is shared, so removing it takes it from everyone. An
  // uploaded spreadsheet belongs to one person, and saying "for all users"
  // there would overstate the stakes of deleting your own file.
  const isShared = kind === "dashboard" || kind === "report"
  // Taking one workbook out of a dataset is a smaller act than deleting the
  // dataset, and the wording has to say so or nobody will use it.
  const isFile = kind === "file"
  // Deleting a folder takes several things at once, so the confirmation has to
  // say what, not just which folder.
  const isFolder = kind === "folder"
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return
        onOpenChange(next)
      }}
    >
      <DialogContent showCloseButton={!busy}>
        <DialogHeader>
          <DialogTitle>
            {isFile ? "Remove this file?" : `Delete this ${kind}?`}
          </DialogTitle>
          <DialogDescription>
            <span className="font-medium text-foreground">{name}</span>
            {isFile
              ? " will be taken out of the dataset and its rows deleted. The other files are untouched."
              : isFolder
                ? " will be removed, along with every dataset and document in it, and any chats that use them as a source."
                : isShared
                  ? " will be removed for all users."
                  : " will be removed, along with any chats that use it as a source."}
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <p className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
            {error}
          </p>
        ) : null}

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm} disabled={busy}>
            {busy ? (
              <>
                <Loader2Icon className="size-4 animate-spin" />
                {isFile ? "Removing…" : "Deleting…"}
              </>
            ) : (
              <>
                <Trash2Icon className="size-4" />
                {isFile ? "Remove" : "Delete"}
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
