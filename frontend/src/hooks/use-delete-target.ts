import * as React from "react"

/**
 * The state one page needs to drive a DeleteDialog over a list of things.
 *
 * Both the dashboards and reports lists want the same four-step shape — open on
 * a target, run, remove the row on success, keep the dialog open with the
 * message on failure — and writing it twice is how the two would drift.
 */
export function useDeleteTarget<T extends { id: string; name: string }>(
  remove: (id: string) => Promise<void>,
  onRemoved: (id: string) => void
) {
  const [target, setTarget] = React.useState<T | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  const confirm = React.useCallback(async () => {
    if (!target) return
    setBusy(true)
    setError(null)
    try {
      await remove(target.id)
      onRemoved(target.id)
      setTarget(null)
    } catch (err) {
      setError(readError(err))
    } finally {
      setBusy(false)
    }
  }, [target, remove, onRemoved])

  return {
    target,
    busy,
    error,
    ask: (next: T) => {
      setError(null)
      setTarget(next)
    },
    close: () => setTarget(null),
    confirm,
  }
}

/** The server's message when there is one — a 403 names the role that is
 *  missing, which is far more useful than "request failed". */
function readError(err: unknown): string {
  if (!(err instanceof Error)) return "Could not delete that."
  try {
    const parsed = JSON.parse(err.message) as { detail?: string }
    if (typeof parsed.detail === "string") return parsed.detail
  } catch {
    // Not JSON — fall through to the raw message.
  }
  return err.message || "Could not delete that."
}
