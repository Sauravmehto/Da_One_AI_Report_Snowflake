import { api } from "@/lib/api"

/**
 * Client for the semantic-layer admin API.
 *
 * Kept apart from lib/api.ts because this is the only writer of the metadata
 * configs — everything else in the app reads them. That separation is worth
 * seeing in the imports.
 */

export type TableDoc = {
  description: string
  notes: string
  key_columns: Record<string, string>
  date_roles: Record<string, string>
}

export type MetricDoc = {
  definition: string
  sql: string
}

export type SemanticDocument = {
  database: string
  display_name: string
  description: string
  join_rules: string
  dashboard_heuristics: string
  tables: Record<string, TableDoc>
  metrics: Record<string, MetricDoc>
}

export type Issue = {
  severity: "error" | "warning"
  field: string
  message: string
}

export type Coverage = {
  total_tables: number
  documented_tables: number
  total_columns: number
  documented_columns: number
  tables_with_date_roles: number
}

export type LayerSummary = {
  database: string
  display_name: string
  is_published: boolean
  has_draft: boolean
  documented_tables: number
}

export type LayerDetail = {
  database: string
  document: SemanticDocument | null
  is_published: boolean
  has_draft: boolean
  coverage: Coverage
  issues: Issue[]
}

/** Thrown when publish is refused; carries the per-field issues. */
export class PublishRejectedError extends Error {
  readonly issues: Issue[]

  constructor(issues: Issue[]) {
    super("The document has errors and cannot be published.")
    this.name = "PublishRejectedError"
    this.issues = issues
  }
}

export function emptyDocument(database: string): SemanticDocument {
  return {
    database: database.toUpperCase(),
    display_name: "",
    description: "",
    join_rules: "",
    dashboard_heuristics: "",
    tables: {},
    metrics: {},
  }
}

export async function fetchLayers(): Promise<LayerSummary[]> {
  return api<LayerSummary[]>("/admin/semantic/databases")
}

export async function fetchLayer(database: string): Promise<LayerDetail> {
  return api<LayerDetail>(`/admin/semantic/${encodeURIComponent(database)}`)
}

export async function saveDraft(
  database: string,
  document: SemanticDocument
): Promise<LayerDetail> {
  return api<LayerDetail>(
    `/admin/semantic/${encodeURIComponent(database)}/draft`,
    { method: "POST", json: document }
  )
}

export async function discardDraft(database: string): Promise<void> {
  await api<void>(`/admin/semantic/${encodeURIComponent(database)}/draft`, {
    method: "DELETE",
  })
}

export async function publishLayer(database: string): Promise<LayerDetail> {
  try {
    return await api<LayerDetail>(
      `/admin/semantic/${encodeURIComponent(database)}/publish`,
      { method: "POST" }
    )
  } catch (err) {
    // The API returns 422 with the full issue list so the editor can point at
    // each offending field, rather than showing one opaque failure.
    const issues = parseIssues(err)
    if (issues) throw new PublishRejectedError(issues)
    throw err
  }
}

function parseIssues(err: unknown): Issue[] | null {
  if (!(err instanceof Error)) return null
  try {
    const body = JSON.parse(err.message) as {
      detail?: { issues?: Issue[] }
    }
    return body.detail?.issues ?? null
  } catch {
    return null
  }
}
