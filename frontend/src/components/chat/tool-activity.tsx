import { Check, ChevronRightIcon, Loader2, MinusIcon, X } from "lucide-react"

import { cn } from "@/lib/utils"

export type ToolActivity = {
  id: string
  name: string
  status: "running" | "ok" | "error" | "stopped"
  detail?: string | null
  /** The arguments the model called the tool with — the SQL it is about to
   *  run, the dashboard it is about to plan. Shown on expand. */
  input?: unknown
}

/** Human labels — tool names are an implementation detail, not UI copy. */
const LABELS: Record<string, string> = {
  get_database_context: "Reading the semantic layer",
  list_tables: "Listing tables",
  run_safe_sql: "Querying the database",
  show_chart: "Building the chart",
  ask_data_question: "Answering from data",
  explain_concept: "Looking up the term",
  plan_dashboard: "Designing the dashboard",
  save_dashboard: "Saving the dashboard",
  list_saved_dashboards: "Listing saved dashboards",
  run_saved_dashboard: "Refreshing dashboard data",
  edit_dashboard: "Preparing the edit",
}

function label(name: string): string {
  return LABELS[name] ?? name.replace(/_/g, " ")
}

/** What a step is willing to show about itself. */
type StepDetail =
  | { kind: "sql"; sql: string }
  | { kind: "panels"; panels: { title: string; sql?: string }[] }
  | { kind: "json"; json: string }

/**
 * Turn a tool's raw arguments into something worth reading.
 *
 * Not a JSON dump for everything: `list_tables` receives `{database}`, which
 * the person already knows and which would make the list noisier for nothing.
 * A step earns a disclosure triangle only when it has real work to show —
 * a query, or the shape of a dashboard about to be built.
 */
function describeInput(input: unknown): StepDetail | null {
  if (!input || typeof input !== "object") return null
  const record = input as Record<string, unknown>

  if (typeof record.sql === "string" && record.sql.trim()) {
    return { kind: "sql", sql: record.sql.trim() }
  }

  if (Array.isArray(record.panels)) {
    const panels = record.panels
      .filter((p): p is Record<string, unknown> => Boolean(p) && typeof p === "object")
      .map((p) => ({
        title: typeof p.title === "string" ? p.title : "Untitled panel",
        sql: typeof p.sql === "string" ? p.sql.trim() : undefined,
      }))
    if (panels.length) return { kind: "panels", panels }
  }

  // Anything else: show it only if there is more than a database name in it.
  const keys = Object.keys(record).filter((k) => k !== "database")
  if (keys.length === 0) return null
  return { kind: "json", json: JSON.stringify(record, null, 2) }
}

const CODE_BLOCK =
  "mt-1.5 max-w-full overflow-x-auto rounded-md border bg-muted/50 p-2.5 " +
  "font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words"

function StepDetailView({ detail }: { detail: StepDetail }) {
  if (detail.kind === "sql") {
    return <pre className={CODE_BLOCK}>{detail.sql}</pre>
  }

  if (detail.kind === "panels") {
    return (
      <div className="mt-1.5 space-y-2">
        {detail.panels.map((panel, i) => (
          <div key={i} className="min-w-0">
            <p className="text-xs font-medium text-foreground">{panel.title}</p>
            {panel.sql ? (
              <pre className={CODE_BLOCK}>{panel.sql}</pre>
            ) : null}
          </div>
        ))}
      </div>
    )
  }

  return <pre className={CODE_BLOCK}>{detail.json}</pre>
}

function StatusIcon({ status }: { status: ToolActivity["status"] }) {
  if (status === "running") {
    return <Loader2 className="size-3.5 shrink-0 animate-spin" aria-hidden />
  }
  if (status === "ok") {
    return <Check className="size-3.5 shrink-0 text-emerald-600" aria-hidden />
  }
  if (status === "stopped") {
    return <MinusIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
  }
  return <X className="size-3.5 shrink-0 text-destructive" aria-hidden />
}

// Width of the status icon + gap column so detail content aligns consistently.
const INDENT_DETAIL = "pl-[2.75rem]"

export function ToolStepRow({ activity }: { activity: ToolActivity }) {
  const detail = describeInput(activity.input)
  const isStopped = activity.status === "stopped"
  const isError = activity.status === "error"

  const text = (
    <span
      className={cn(
        "leading-snug",
        isError && "text-destructive",
        isStopped && "text-muted-foreground"
      )}
    >
      {label(activity.name)}
      {activity.detail ? (
        <span className="text-muted-foreground/60"> · {activity.detail}</span>
      ) : null}
    </span>
  )

  if (!detail) {
    return (
      <div className="flex items-center gap-2 py-0.5 text-xs text-muted-foreground">
        <StatusIcon status={activity.status} />
        <span className="size-3.5 shrink-0" aria-hidden />
        {text}
      </div>
    )
  }

  return (
    <details className="group min-w-0 text-xs text-muted-foreground">
      <summary className="flex cursor-pointer list-none items-center gap-2 py-0.5 hover:text-foreground [&::-webkit-details-marker]:hidden">
        <StatusIcon status={activity.status} />
        <ChevronRightIcon
          className="size-3.5 shrink-0 transition-transform group-open:rotate-90"
          aria-hidden
        />
        {text}
      </summary>
      <div className={cn("min-w-0 pt-1", INDENT_DETAIL)}>
        <StepDetailView detail={detail} />
      </div>
    </details>
  )
}

export function ToolActivityList({
  activities,
  className,
}: {
  activities: ToolActivity[]
  className?: string
}) {
  if (activities.length === 0) return null

  return (
    <div className={cn("mb-3 flex w-full min-w-0 flex-col gap-1.5", className)}>
      {activities.map((activity) => (
        <ToolStepRow key={activity.id} activity={activity} />
      ))}
    </div>
  )
}
