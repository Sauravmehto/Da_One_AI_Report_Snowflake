import { cn } from "@/lib/utils"

/** Quiet executive line under the KPI band — one job, no chrome. */
export function DashboardNarrative({
  text,
  className,
}: {
  text: string
  className?: string
}) {
  return (
    <p
      className={cn(
        "max-w-4xl text-[13px] leading-relaxed text-muted-foreground",
        className
      )}
    >
      {text}
    </p>
  )
}
