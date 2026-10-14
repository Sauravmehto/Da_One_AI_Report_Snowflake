import { CheckIcon } from "lucide-react"

/**
 * The ring-then-tick success mark.
 *
 * Extracted from PublishDialog so saving a report gets the same confirmation as
 * publishing a semantic layer. Both are moments where the page looks identical
 * before and after, and a silent success leaves you wondering whether it worked.
 *
 * The sequencing is the point: the ring draws, then the tick lands. A static
 * green check reads as a status icon; this reads as something completing.
 */
export function SuccessCheck() {
  return (
    <div className="relative flex size-20 items-center justify-center">
      <svg viewBox="0 0 52 52" className="absolute size-20 -rotate-90">
        <circle
          cx="26"
          cy="26"
          r="24"
          fill="none"
          strokeWidth="2.5"
          className="stroke-emerald-500/25"
        />
        <circle
          cx="26"
          cy="26"
          r="24"
          fill="none"
          strokeWidth="2.5"
          strokeLinecap="round"
          pathLength={1}
          className="animate-publish-ring stroke-emerald-500"
        />
      </svg>
      <span className="animate-publish-tick flex size-11 items-center justify-center rounded-full bg-emerald-500 text-white">
        <CheckIcon className="size-6" strokeWidth={3} />
      </span>
    </div>
  )
}
