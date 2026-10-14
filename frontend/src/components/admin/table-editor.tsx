import * as React from "react"
import { CalendarIcon, XIcon } from "lucide-react"

import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchTableDetail, type ColumnDetail } from "@/lib/api"
import type { Issue, TableDoc } from "@/lib/semantic-api"
import { cn } from "@/lib/utils"

/**
 * Roles offered in the picker. Not a closed list — the field accepts anything
 * in snake_case, because the planner is shown the roles a table declares and
 * asks for one by name. Real estate needs `acquisition_date`; PE needs
 * `warrant_expiry_date`. These are just the common ones.
 */
const SUGGESTED_ROLES = [
  "snapshot_date",
  "acquisition_date",
  "closing_date",
  "effective_date",
  "entry_date",
  "inception_date",
  "last_payment_date",
  "maturity_date",
  "next_payment_date",
  "origination_date",
  "payment_date",
  "payment_received_date",
  "reporting_date",
  "transaction_date",
  "valuation_date",
]

const DATE_TYPES = ["DATE", "TIMESTAMP", "DATETIME"]

function isDateType(dataType: string): boolean {
  return DATE_TYPES.some((t) => dataType.toUpperCase().includes(t))
}

export function TableEditor({
  database,
  fullName,
  doc,
  issues,
  onChange,
}: {
  database: string
  fullName: string
  doc: TableDoc
  issues: Issue[]
  onChange: (next: TableDoc) => void
}) {
  const [columns, setColumns] = React.useState<ColumnDetail[] | null>(null)
  const [loadError, setLoadError] = React.useState<string | null>(null)

  const [, schema, table] = fullName.split(".")

  React.useEffect(() => {
    let cancelled = false
    setColumns(null)
    setLoadError(null)
    void (async () => {
      try {
        const detail = await fetchTableDetail(database, schema, table)
        if (!cancelled) setColumns(detail.columns)
      } catch (err) {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : "Failed to load")
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [database, schema, table])

  function patch(changes: Partial<TableDoc>) {
    onChange({ ...doc, ...changes })
  }

  function setColumnDescription(column: string, value: string) {
    const next = { ...doc.key_columns }
    if (value.trim()) next[column] = value
    else delete next[column]
    patch({ key_columns: next })
  }

  function setDateRole(column: string, role: string) {
    const next = { ...doc.date_roles }
    if (role.trim()) next[column] = role.trim()
    else delete next[column]
    patch({ date_roles: next })
  }

  const dateColumns = (columns ?? []).filter((c) => isDateType(c.data_type))
  const documented = (columns ?? []).filter(
    (c) => (doc.key_columns[c.name] ?? "").trim().length > 0
  ).length

  return (
    <div className="space-y-6">
      <header>
        <h2 className="font-mono text-sm">{fullName}</h2>
        {columns ? (
          <p className="text-xs text-muted-foreground">
            {documented} of {columns.length} columns described
            {Object.keys(doc.date_roles).length > 0
              ? ` · ${Object.keys(doc.date_roles).length} date role(s)`
              : ""}
          </p>
        ) : null}
      </header>

      <Field label="Description" hint="What one row represents.">
        <Textarea
          value={doc.description}
          onChange={(e) => patch({ description: e.target.value })}
          rows={2}
          placeholder="Each row is one private credit investment."
        />
      </Field>

      <Field
        label="Notes"
        hint="Gotchas the schema cannot express: enum values, text-encoded numbers, which key to join on."
      >
        <Textarea
          value={doc.notes}
          onChange={(e) => patch({ notes: e.target.value })}
          rows={2}
          placeholder="Joins to PRIVATE_CREDIT on SECURITY_ID. LOAN_STATUS values: Active, Watch-list…"
        />
      </Field>

      {/* Date roles first: this is the section whose mistakes are silent. A
          wrong role here does not error anywhere — the planner simply stops
          offering date filters, or binds to a column that matches nothing. */}
      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <CalendarIcon className="size-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">Date roles</h3>
        </div>
        <p className="text-xs text-muted-foreground">
          Date parameters bind to these columns by role, never by guessing a
          name. A table with no roles cannot be filtered by date at all.
        </p>

        {columns === null ? (
          <Skeleton className="h-9 w-full" />
        ) : dateColumns.length === 0 ? (
          <p className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
            No date or timestamp columns on this table.
          </p>
        ) : (
          <ul className="space-y-2">
            {dateColumns.map((column) => {
              const role = doc.date_roles[column.name] ?? ""
              const issue = issues.find((i) =>
                i.field.endsWith(`date_roles.${column.name}`)
              )
              return (
                <li key={column.name} className="flex items-start gap-2">
                  <span className="w-52 shrink-0 truncate pt-2 font-mono text-xs">
                    {column.name}
                    <span className="ml-1.5 text-muted-foreground">
                      {column.data_type}
                    </span>
                  </span>
                  <div className="min-w-0 flex-1">
                    <Input
                      list="semantic-date-roles"
                      value={role}
                      onChange={(e) => setDateRole(column.name, e.target.value)}
                      placeholder="unbound"
                      className={cn(
                        "h-9 font-mono text-xs",
                        issue?.severity === "error" && "border-destructive"
                      )}
                    />
                    {issue ? (
                      <p
                        className={cn(
                          "mt-1 text-xs",
                          issue.severity === "error"
                            ? "text-destructive"
                            : "text-amber-600 dark:text-amber-400"
                        )}
                      >
                        {issue.message}
                      </p>
                    ) : null}
                  </div>
                  {role ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      aria-label={`Clear role for ${column.name}`}
                      onClick={() => setDateRole(column.name, "")}
                    >
                      <XIcon className="size-4" />
                    </Button>
                  ) : null}
                </li>
              )
            })}
          </ul>
        )}

        <datalist id="semantic-date-roles">
          {SUGGESTED_ROLES.map((r) => (
            <option key={r} value={r} />
          ))}
        </datalist>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-medium">Columns</h3>
        <p className="text-xs text-muted-foreground">
          What the column means, in the business's words. Blank columns are
          simply not described to the model.
        </p>

        {loadError ? (
          <p className="text-sm text-destructive">{loadError}</p>
        ) : columns === null ? (
          <div className="space-y-2">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
          </div>
        ) : (
          <ul className="space-y-2">
            {columns.map((column) => (
              <li key={column.name} className="flex items-start gap-2">
                <span className="w-52 shrink-0 truncate pt-2 font-mono text-xs">
                  {column.name}
                  <span className="ml-1.5 text-muted-foreground">
                    {column.data_type}
                  </span>
                </span>
                <Input
                  value={doc.key_columns[column.name] ?? ""}
                  onChange={(e) =>
                    setColumnDescription(column.name, e.target.value)
                  }
                  placeholder="Not described"
                  className="h-9 text-sm"
                />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium">{label}</label>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
      {children}
    </div>
  )
}
