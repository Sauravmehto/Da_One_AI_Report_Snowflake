import * as React from "react"
import { useNavigate } from "react-router-dom"
import { InfoIcon, SparklesIcon } from "lucide-react"

import {
  fetchTableDetail,
  fetchTablePreview,
  type TableDetail as TableDetailData,
  type TablePreview as TablePreviewData,
} from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

type TableDetailProps = {
  database: string
  schema: string
  table: string
}

/**
 * Make Table's own container the scroll region and let it fill the panel.
 * Table already owns `overflow-x-auto`; wrapping it in a second scroller would
 * put the sticky header's containing block on the inner element, where it has
 * no room to move and therefore never sticks.
 */
const SCROLL_TABLE =
  "h-full overflow-hidden rounded-lg border " +
  "[&_[data-slot=table-container]]:h-full " +
  "[&_[data-slot=table-container]]:overscroll-contain"

/**
 * Sticky is applied per-cell rather than to <thead>: a sticky thead is still
 * unevenly supported, and the row's bottom border stops painting once stuck,
 * so the rule is redrawn as an inset shadow.
 */
const STICKY_HEAD =
  "[&_th]:sticky [&_th]:top-0 [&_th]:z-10 [&_th]:bg-background " +
  "[&_th]:shadow-[inset_0_-1px_0_var(--border)]"

export function TableDetail({ database, schema, table }: TableDetailProps) {
  const navigate = useNavigate()
  const [detail, setDetail] = React.useState<TableDetailData | null>(null)
  const [isLoading, setIsLoading] = React.useState(true)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    let isMounted = true
    setIsLoading(true)
    setError(null)
    setDetail(null)

    fetchTableDetail(database, schema, table)
      .then((result) => {
        if (isMounted) {
          setDetail(result)
        }
      })
      .catch((err: unknown) => {
        if (isMounted) {
          setError(err instanceof Error ? err.message : "Failed to load table")
        }
      })
      .finally(() => {
        if (isMounted) {
          setIsLoading(false)
        }
      })

    return () => {
      isMounted = false
    }
  }, [database, schema, table])

  if (isLoading) {
    return (
      <div className="space-y-4 p-6">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-4 w-full max-w-lg" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  if (error) {
    return <p className="p-6 text-sm text-destructive">{error}</p>
  }

  if (!detail) {
    return null
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      {/* Header — pinned. Everything down to the tab strip stays put; only the
          table below scrolls. Capped and scrollable in its own right, because
          a long `notes` block would otherwise push the tabs off the pane. */}
      <div className="scrollbar-slim max-h-[40%] shrink-0 overflow-y-auto overscroll-contain px-6 pt-6">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-lg font-semibold">{detail.name}</h2>
              {detail.is_documented ? (
                <Badge variant="secondary">Documented</Badge>
              ) : (
                <Badge variant="outline">Not documented</Badge>
              )}
            </div>
            <p className="mt-0.5 font-mono text-xs text-muted-foreground">
              {detail.full_name}
            </p>
          </div>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5"
                onClick={() =>
                  navigate("/", {
                    state: {
                      prompt: `Give me an overview of the ${detail.schema}.${detail.name} table and show a sample of the data.`,
                      database,
                    },
                  })
                }
              >
                <SparklesIcon />
                Ask about this table
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              Start a chat scoped to this table
            </TooltipContent>
          </Tooltip>
        </div>

        {detail.description ? (
          <p className="mt-3 text-sm text-muted-foreground">
            {detail.description}
          </p>
        ) : null}

        {/* Notes / gotchas callout */}
        {detail.notes ? (
          <div className="mt-3 flex gap-2 rounded-lg border border-border bg-muted/40 p-3 text-sm">
            <InfoIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <p className="text-muted-foreground">{detail.notes}</p>
          </div>
        ) : null}
      </div>

      {/* Tabs. The strip is pinned with the header; each panel is the scroll
          region, so the table's own header row can stick to the top of it. */}
      <Tabs
        defaultValue="columns"
        className="flex min-h-0 min-w-0 flex-1 flex-col gap-0 overflow-hidden"
      >
        <TabsList className="mt-5 ml-6 w-fit shrink-0">
          <TabsTrigger value="columns">
            Columns
            <Badge variant="secondary" className="ml-1.5">
              {detail.columns.length}
            </Badge>
          </TabsTrigger>
          <TabsTrigger value="preview">Preview</TabsTrigger>
        </TabsList>

        <TabsContent
          value="columns"
          className="mt-3 min-h-0 min-w-0 flex-1 overflow-hidden px-6 pb-6"
        >
          {/* The height goes on Table's own container (it already owns the
              overflow) rather than on a wrapper — otherwise the sticky header
              resolves against the inner element and never sticks. */}
          <div className={SCROLL_TABLE}>
            <Table>
              <TableHeader className={STICKY_HEAD}>
                <TableRow>
                  <TableHead>Column</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Nullable</TableHead>
                  <TableHead>Meaning</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {detail.columns.map((column) => (
                  <TableRow key={column.name}>
                    <TableCell className="font-medium">{column.name}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {column.data_type}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {column.nullable ? "Yes" : "No"}
                    </TableCell>
                    <TableCell className="max-w-md whitespace-normal text-muted-foreground">
                      {column.description || "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </TabsContent>

        <TabsContent
          value="preview"
          className="mt-3 min-h-0 min-w-0 flex-1 overflow-hidden px-6 pb-6"
        >
          <PreviewTab database={database} schema={schema} table={table} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function PreviewTab({ database, schema, table }: TableDetailProps) {
  const [preview, setPreview] = React.useState<TablePreviewData | null>(null)
  const [isLoading, setIsLoading] = React.useState(true)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    let isMounted = true
    setIsLoading(true)
    setError(null)
    setPreview(null)

    fetchTablePreview(database, schema, table)
      .then((result) => {
        if (isMounted) {
          setPreview(result)
        }
      })
      .catch((err: unknown) => {
        if (isMounted) {
          setError(
            err instanceof Error ? err.message : "Failed to load preview"
          )
        }
      })
      .finally(() => {
        if (isMounted) {
          setIsLoading(false)
        }
      })

    return () => {
      isMounted = false
    }
  }, [database, schema, table])

  if (isLoading) {
    return <Skeleton className="h-48 w-full" />
  }

  if (error) {
    return <p className="text-sm text-destructive">{error}</p>
  }

  if (!preview || preview.rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No rows to preview.</p>
  }

  return (
    // The caption is pinned below the table rather than trailing it, so the
    // row cap stays visible while you scroll through the sample.
    <div className="flex h-full min-h-0 flex-col gap-2">
      <div className={cn(SCROLL_TABLE, "min-h-0 flex-1")}>
        <Table>
          <TableHeader className={STICKY_HEAD}>
            <TableRow>
              {preview.columns.map((column) => (
                <TableHead key={column} className="whitespace-nowrap">
                  {column}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {preview.rows.map((row, rowIndex) => (
              <TableRow key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <TableCell key={cellIndex} className="max-w-xs truncate">
                    {cell === null ? (
                      <span className="text-muted-foreground italic">null</span>
                    ) : (
                      String(cell)
                    )}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <p className="shrink-0 text-xs text-muted-foreground">
        Showing up to {preview.row_limit} rows.
      </p>
    </div>
  )
}
