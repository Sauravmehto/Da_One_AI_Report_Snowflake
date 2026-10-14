import type { ChatData } from "@/lib/api"
import type { TableWidgetConfig, WidgetRenderProps } from "@/lib/report-types"

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

type QueryResultsTableProps = {
  data: ChatData
  className?: string
}

export function QueryResultsTable({ data, className }: QueryResultsTableProps) {
  if (data.rows.length === 0) {
    return null
  }

  return (
    <div className={className}>
      <div className="max-w-full overflow-x-auto rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              {data.columns.map((column) => (
                <TableHead key={column} className="whitespace-nowrap">
                  {column}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.rows.map((row, rowIndex) => (
              <TableRow key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <TableCell
                    key={cellIndex}
                    className="max-w-48 truncate whitespace-nowrap"
                  >
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
      <p className="mt-2 text-xs text-muted-foreground">
        {data.rows.length} row{data.rows.length === 1 ? "" : "s"} returned.
      </p>
    </div>
  )
}

export function TableWidget({ widget }: WidgetRenderProps) {
  const w = widget as TableWidgetConfig
  return (
    <div className="flex flex-col gap-4 rounded-xl border bg-card p-6 shadow-sm">
      <div>
        <h3 className="font-semibold leading-none">{w.title}</h3>
        {w.description ? (
          <p className="mt-1 text-sm text-muted-foreground">{w.description}</p>
        ) : null}
      </div>
      <QueryResultsTable data={{ columns: w.columns, rows: w.rows }} />
    </div>
  )
}
