const API_BASE = "/api/v1"

type ApiOptions = RequestInit & {
  json?: unknown
}

// Global 401 handler — components can subscribe to be notified when the session
// expires so they can redirect to login without coupling api.ts to React Router.
type UnauthorizedListener = () => void
const _unauthorizedListeners = new Set<UnauthorizedListener>()

export function onUnauthorized(fn: UnauthorizedListener): () => void {
  _unauthorizedListeners.add(fn)
  return () => _unauthorizedListeners.delete(fn)
}

function _notifyUnauthorized() {
  _unauthorizedListeners.forEach((fn) => fn())
}

export async function api<T>(
  path: string,
  options: ApiOptions = {}
): Promise<T> {
  const { json, headers, ...rest } = options

  const response = await fetch(`${API_BASE}${path}`, {
    ...rest,
    credentials: "include",
    headers: {
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  })

  if (response.status === 401) {
    _notifyUnauthorized()
    throw new Error("Session expired. Please log in again.")
  }

  if (!response.ok) {
    const message = await response.text()
    throw new Error(message || `Request failed with status ${response.status}`)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return response.json() as Promise<T>
}

// ── Auth ──────────────────────────────────────────────────────────────────────

export type AuthUser = {
  snowflake_user: string
  role: string | null
  /**
   * Whether this user's Snowflake role may author semantic layers.
   *
   * For hiding UI only. The server checks the role on every admin route, so
   * this is a courtesy to the user, not a security boundary — treating it as
   * one would put the decision in the client, where anyone can change it.
   */
  is_admin: boolean
}

export function getLoginUrl(): string {
  return `${API_BASE}/auth/login`
}

export async function fetchCurrentUser(): Promise<AuthUser> {
  return api<AuthUser>("/auth/me")
}

export type AvailableRoles = {
  roles: string[]
  current: string | null
}

export async function fetchAvailableRoles(): Promise<AvailableRoles> {
  return api<AvailableRoles>("/auth/roles")
}

export async function setActiveRole(role: string): Promise<AuthUser> {
  return api<AuthUser>("/auth/role", {
    method: "POST",
    json: { role },
  })
}

/**
 * Which sign-in methods this deployment offers.
 *
 * Indexed rather than a fixed shape: identity is expected to move off Snowflake,
 * and a newer backend reporting a method this build has never heard of should
 * not be a type error. lib/auth-methods.ts decides what each key renders as.
 */
export type AuthCapabilities = {
  oauth: boolean
  dev_login: boolean
} & Record<string, boolean>

/** Which sign-in methods the backend actually offers, so the UI never shows a
 * button that cannot work. `dev_login` is false in any real deployment. */
export async function fetchAuthCapabilities(): Promise<AuthCapabilities> {
  return api<AuthCapabilities>("/auth/capabilities")
}

/** TEMPORARY dev-only sign-in (key-pair service account) — removed once the
 * Snowflake OAuth integration is live. */
export async function devLogin(): Promise<AuthUser> {
  return api<AuthUser>("/auth/dev-login", { method: "POST" })
}

// ── Conversations ────────────────────────────────────────────────────────────

export type ConversationSummary = {
  conversation_id: string
  title: string | null
  database: string | null
  /** Set when the thread belongs to a dashboard's side panel. */
  dashboard_id: string | null
  pinned: boolean
  message_count: number
  created_at: string
  updated_at: string
}

/** One stored turn, in the Anthropic content-block shape the server persists. */
export type StoredMessage = {
  role: "user" | "assistant"
  content: Array<Record<string, unknown>>
  created_at: string
}

/** A rendered chart, kept out of `messages` so replay never re-feeds its rows. */
export type StoredArtifact = {
  kind: "chart"
  panel: Record<string, unknown>
}

export type StoredConversation = {
  conversation_id: string
  owner: string
  database: string | null
  title: string | null
  messages: StoredMessage[]
  /** Keyed by the tool_use id that produced the artifact. */
  artifacts?: Record<string, StoredArtifact>
}

/**
 * Conversations for the signed-in user.
 *
 * With no argument this is the sidebar's list and EXCLUDES dashboard-scoped
 * threads — they belong to their dashboard, and a handful of questions per
 * dashboard would otherwise bury the real chat threads. Pass a dashboard id to
 * get that dashboard's threads instead.
 */
export async function fetchConversations(
  dashboardId?: string,
  /** "dashboard_edit" lists the Edit panel assistant's threads instead of the
   *  Ask panel's. */
  kind: "chat" | "dashboard_edit" = "chat"
): Promise<ConversationSummary[]> {
  const params = new URLSearchParams()
  if (dashboardId) params.set("dashboard_id", dashboardId)
  if (kind !== "chat") params.set("kind", kind)
  const query = params.toString() ? `?${params}` : ""
  return api<ConversationSummary[]>(`/agent/conversations${query}`)
}

export async function fetchConversation(
  id: string
): Promise<StoredConversation> {
  return api<StoredConversation>(
    `/agent/conversations/${encodeURIComponent(id)}`
  )
}

export async function deleteConversation(id: string): Promise<void> {
  await api<void>(`/agent/conversations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })
}

export async function updateConversation(
  id: string,
  patch: { title?: string; pinned?: boolean }
): Promise<ConversationSummary> {
  return api<ConversationSummary>(
    `/agent/conversations/${encodeURIComponent(id)}`,
    { method: "PATCH", json: patch }
  )
}

// ── Saved dashboards ─────────────────────────────────────────────────────────

export type SavedDashboardSummary = {
  report_id: string
  report_title: string
  version: number
  /** Who created it — the author of version 1, not of the latest edit. */
  created_by: string | null
  created_at?: string | null
  /** Who last changed it. Every edit, including a drag of the layout, writes a
   *  new version under the editor's name; that is this, not `created_by`. */
  updated_by?: string | null
  updated_at?: string | null
  /** The database it was built from. Null for older rows saved before the
   *  column existed, and for a plan that names no documented database. */
  database?: string | null
  /** The folder it is listed under: a folder somebody moved it to, else its
   *  database, else one bucket for uploads. */
  data_group?: string | null
  /** Set only when somebody deliberately filed it somewhere else. */
  data_group_override?: string | null
  last_run_at: string | null
  run_count: number
  /** Present for dashboards saved from uploaded files/folders. */
  source_type?: "snowflake" | "file"
  source_key?: string | null
}

export type DashboardParameter = {
  name: string
  type: string
  default?: unknown
  options?: string[]
  panels?: string[]
}

export type DashboardRun = {
  report_id: string
  title: string | null
  run_count: number
  last_run_at: string | null
  parameters: DashboardParameter[]
  parameter_values: Record<string, unknown>
  panels: Array<{
    title: string
    chart_type: string | null
    x: string | null
    y: string | string[] | null
    color: string | null
    size: string | null
    orientation: string | null
    display: string | null
    columns: string[]
    rows: unknown[][]
    row_count: number
    /** The engine's row cap was reached, so rows are missing. */
    truncated?: boolean | null
    error?: string | null
  }>
}

/** One panel as it is STORED — the render config plus the SQL behind it. The
 *  run endpoints deliberately return the config and the rows but never the
 *  SQL, so this shape only ever comes from the admin-only config endpoint. */
import type { PanelFormat } from "@/lib/chart-standard"
import type { ColumnFormats } from "@/lib/dashboard-format"

export type DashboardPanelConfig = {
  title: string | null
  chart_type: string | null
  x: string | null
  y: string | string[] | null
  color: string | null
  size: string | null
  orientation: string | null
  display: string | null
  sql: string | null
  /* Round-tripped, not edited here. They MUST be declared even so: the server
     rebuilds each panel from its own known-field list, so a panel object built
     from this type without them comes back with its tab, its size on the grid
     and its column formats set to null. They survived only because the objects
     fetched from the API happened to carry them at runtime. */
  section?: string | null
  span?: number | null
  row_span?: number | null
  column_formats?: ColumnFormats | null
  /* Presentation. Absent on every panel saved before these existed, and absent
     is the default in both cases, so an untouched panel renders as it always
     has. Declared here for the same reason as the layout fields above: the
     server rebuilds each panel from its own known-field list, so a field this
     type omits is dropped on save. */
  zero_baseline?: boolean | null
  palette?: string | null
  /* Label and axis settings. Same rule as above: absent means the chart
     standard, and it must be declared or a save would drop it. */
  format?: PanelFormat | null
}

export type DashboardConfig = {
  report_id: string
  title: string | null
  description: string
  database: string | null
  parameters: DashboardParameter[]
  panels: DashboardPanelConfig[]
}

/** The stored configuration, for editing. Administrators only — the server
 *  enforces it, because this is the only surface exposing panel SQL. */
export async function fetchDashboardConfig(
  reportId: string
): Promise<DashboardConfig> {
  return api<DashboardConfig>(
    `/dashboards/${encodeURIComponent(reportId)}/config`
  )
}

/**
 * Save an edited configuration as a new version.
 *
 * Every panel is re-validated server-side — read-only SQL check plus a zero-row
 * dry run — and one bad panel rejects the whole save, so an edit can never
 * quietly shrink a dashboard. The previous version is kept and demoted, so a
 * mistake is a revert rather than a loss.
 */
export async function updateDashboard(
  reportId: string,
  plan: {
    title?: string | null
    description?: string
    parameters?: DashboardParameter[]
    panels: DashboardPanelConfig[]
  },
  editNote = "",
  /** The Edit panel assistant's thread, when it was used: the save is noted
   *  there so the thread shows which draft became which version. */
  assistantConversationId?: string | null
): Promise<{
  report_id: string
  version: number
  saved: boolean
  /** Changed panels that will not draw what they name, e.g. a column the
   *  query does not return. The save has still happened. */
  warnings?: string[]
}> {
  return api(`/dashboards/${encodeURIComponent(reportId)}`, {
    method: "PUT",
    json: {
      plan,
      edit_note: editNote,
      ...(assistantConversationId
        ? { assistant_conversation_id: assistantConversationId }
        : {}),
    },
  })
}

/** A draft as the Edit panel holds it. */
export type DashboardDraft = {
  title: string
  panels: DashboardPanelConfig[]
  parameters: DashboardParameter[]
}

export type DashboardAssistResult = {
  conversation_id: string
  /** applied: `plan` is the updated draft. no_change: advice or a question.
   *  needs_clarification: the assistant is asking. rejected: the change
   *  failed its checks and nothing was applied. */
  status: "applied" | "no_change" | "needs_clarification" | "rejected"
  reply: string
  /** The assistant's whole turn as shown in the panel and stored. */
  message: string
  plan?: DashboardDraft
  /** Per panel of `plan`: the index of the draft panel it came from, or null
   *  for a panel the assistant added. */
  origins?: (number | null)[]
  /** Per panel of `plan`: whether the assistant changed it. */
  changed?: boolean[]
  changes?: string[]
  problems?: string[]
}

/** Record an Undo, or an answer the panel could not apply, in the assistant's
 *  thread, so it stops describing a change the draft no longer has. */
export async function noteDashboardAssist(
  reportId: string,
  conversationId: string,
  event: "undo" | "not_applied"
): Promise<{ conversation_id: string; message: string }> {
  return api(`/dashboards/${encodeURIComponent(reportId)}/assist/note`, {
    method: "POST",
    json: { conversation_id: conversationId, event },
  })
}

/**
 * One message to the Edit panel's assistant about the draft on screen.
 *
 * Saves nothing. A change comes back as an updated draft for the panel to show;
 * saving it is still "Save and refresh".
 */
export async function assistDashboardEdit(
  reportId: string,
  body: {
    message: string
    plan: DashboardDraft
    conversation_id?: string | null
  },
  signal?: AbortSignal
): Promise<DashboardAssistResult> {
  return api(`/dashboards/${encodeURIComponent(reportId)}/assist`, {
    method: "POST",
    json: body,
    signal,
  })
}

export type PanelLayout = {
  title: string
  section?: string | null
  span?: number | null
  row_span?: number | null
}

/**
 * Save which tab each panel sits on and how much of the grid it occupies.
 *
 * Separate from updateDashboard because it cannot touch SQL: that endpoint
 * re-validates and dry-runs every panel, which a drag has no reason to wait
 * for. Still versioned server-side, so a layout change is revertible — which
 * is why the page saves on an explicit action rather than on every drag.
 */
export async function updateDashboardLayout(
  reportId: string,
  layout: PanelLayout[]
): Promise<{ report_id: string; version?: number; saved: boolean }> {
  return api(`/dashboards/${encodeURIComponent(reportId)}/layout`, {
    method: "PUT",
    json: { layout },
  })
}

/**
 * A plain-English explanation of one chart, for its info button.
 *
 * The descriptor is sent from here rather than looked up server-side because
 * the panel is already on screen — so opening the button costs no warehouse
 * query. Only NAMES travel: title, chart type, column names. Never rows.
 */
export async function describePanel(input: {
  title: string
  chart_type: string
  x?: string | null
  y?: string | string[] | null
  columns?: string[]
  dashboard_title?: string | null
}): Promise<{ description: string; cached: boolean }> {
  return api("/dashboards/panel-description", { method: "POST", json: input })
}

/**
 * Move a dashboard into a folder. An empty name sends it back to its database.
 *
 * Not an edit: this writes no new version, because where a dashboard is filed
 * says nothing about what it shows.
 */
export async function setDashboardGroup(
  reportId: string,
  group: string | null
): Promise<{ report_id: string; group: string | null }> {
  return api(`/dashboards/${encodeURIComponent(reportId)}/group`, {
    method: "PUT",
    json: { group: group ?? "" },
  })
}

export async function fetchSavedDashboards(): Promise<SavedDashboardSummary[]> {
  const res = await api<{ count: number; dashboards: SavedDashboardSummary[] }>(
    "/dashboards"
  )
  return res.dashboards
}

/** Live run at saved defaults. Each call re-executes the panels. */
export async function fetchDashboard(reportId: string): Promise<DashboardRun> {
  return api<DashboardRun>(`/dashboards/${encodeURIComponent(reportId)}`)
}

/** Live run with explicit parameter values — never edits or versions. */
export async function runDashboard(
  reportId: string,
  parameterValues: Record<string, unknown>
): Promise<DashboardRun> {
  return api<DashboardRun>(`/dashboards/${encodeURIComponent(reportId)}/run`, {
    method: "POST",
    json: { parameter_values: parameterValues },
  })
}

/**
 * Download the live dashboard as Excel or PDF.
 * Triggers a fresh run server-side, then streams the file.
 */
export async function downloadDashboardExport(
  reportId: string,
  format: "excel" | "pdf" = "excel",
  /** The summary already on screen, so the PDF carries it without the server
   *  paying for a second model call to regenerate the same paragraph. */
  narrative?: string | null,
  /** {panel title: PNG data URL}, a picture of each chart as the page draws it. */
  panelImages?: Record<string, string> | null,
  /** {panel title: table}, each table's headers and cells as the page writes them. */
  panelTables?: Record<string, { headers: string[]; numeric: boolean[]; rows: string[][] }> | null
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/dashboards/${encodeURIComponent(reportId)}/export?format=${format}`,
    narrative ||
      (panelImages && Object.keys(panelImages).length > 0) ||
      (panelTables && Object.keys(panelTables).length > 0)
      ? {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            narrative,
            panel_images: panelImages ?? undefined,
            panel_tables: panelTables ?? undefined,
          }),
        }
      : { credentials: "include" }
  )
  if (response.status === 401) {
    _notifyUnauthorized()
    throw new Error("Session expired. Please log in again.")
  }
  if (!response.ok) {
    const message = await response.text()
    throw new Error(message || `Export failed (${response.status})`)
  }
  const blob = await response.blob()
  const disposition = response.headers.get("Content-Disposition") ?? ""
  const match = /filename="([^"]+)"/i.exec(disposition)
  const fallback = format === "pdf" ? `dashboard_${reportId}.pdf` : `dashboard_${reportId}.xlsx`
  const filename = match?.[1] ?? fallback
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export type DashboardNarrativeRequest = {
  title: string
  /** The database's reporting currency, for figures that do not name one. */
  default_currency?: string | null
  kpis: Array<{ metric: unknown; value: unknown; format?: unknown }>
  /* The server turns each chart into its figures -- first, latest, high, low
     or top and bottom -- so it needs the rows and how the chart reads them.
     The rows are what is already on screen: at most the row limit per panel. */
  panels: Array<{
    title?: string | null
    chart_type?: string | null
    x?: string | null
    y?: string | string[] | null
    color?: string | null
    columns?: string[]
    rows?: unknown[][]
    column_formats?: ColumnFormats | null
    default_currency?: string | null
    row_count?: number
    /** Capped by the engine, so the newest rows are absent from sample_rows. */
    truncated?: boolean
  }>
}

/** AI executive narrative for the current dashboard view. */
export async function fetchDashboardNarrative(
  reportId: string,
  body: DashboardNarrativeRequest
): Promise<string> {
  const res = await api<{ narrative: string }>(
    `/dashboards/${encodeURIComponent(reportId)}/narrative`,
    { method: "POST", json: body }
  )
  return res.narrative
}

/**
 * Retire a saved dashboard. Administrators only — the server enforces it.
 *
 * The row is deactivated rather than dropped, so this is recoverable by
 * whoever administers the warehouse. Nothing in the UI says so, because
 * "recoverable by an administrator" is not a promise worth making to the
 * person clicking Delete.
 */
export async function deleteDashboard(reportId: string): Promise<void> {
  await api<unknown>(`/dashboards/${encodeURIComponent(reportId)}`, {
    method: "DELETE",
  })
}

export async function logout(): Promise<void> {
  await api<void>("/auth/logout", { method: "POST" })
}

// ── Databases & Explore ─────────────────────────────────────────────────────

export type DatabaseSummary = {
  name: string
  display_name: string
  description: string
  is_documented: boolean
  /**
   * Where this source's data lives. "warehouse" is a Snowflake database;
   * "spreadsheet" is a workbook the user uploaded, addressed as "EXCEL:<id>".
   *
   * Optional so every existing caller and every /databases response stays valid
   * without change — absent means warehouse.
   */
  kind?: "warehouse" | "spreadsheet"
}

export type TableSummary = {
  schema: string
  name: string
  full_name: string
  table_type: string
  row_count: number | null
  description: string
  is_documented: boolean
}

export type ColumnDetail = {
  name: string
  data_type: string
  nullable: boolean
  description: string
}

export type TableDetail = {
  database: string
  schema: string
  name: string
  full_name: string
  description: string
  notes: string
  is_documented: boolean
  columns: ColumnDetail[]
}

export type TablePreview = {
  columns: string[]
  rows: (string | number | boolean | null)[][]
  row_limit: number
}

export async function fetchDatabases(): Promise<DatabaseSummary[]> {
  return api<DatabaseSummary[]>("/databases")
}

export async function fetchTables(database: string): Promise<TableSummary[]> {
  return api<TableSummary[]>(
    `/databases/${encodeURIComponent(database)}/tables`
  )
}

export async function fetchTableDetail(
  database: string,
  schema: string,
  table: string
): Promise<TableDetail> {
  return api<TableDetail>(
    `/databases/${encodeURIComponent(database)}/tables/${encodeURIComponent(
      schema
    )}/${encodeURIComponent(table)}`
  )
}

export async function fetchTablePreview(
  database: string,
  schema: string,
  table: string,
  limit = 50
): Promise<TablePreview> {
  return api<TablePreview>(
    `/databases/${encodeURIComponent(database)}/tables/${encodeURIComponent(
      schema
    )}/${encodeURIComponent(table)}/preview?limit=${limit}`
  )
}

// ── Chat (AI query flow) ───────────────────────────────────────────────────

export type ChatData = {
  columns: string[]
  rows: (string | number | boolean | null)[][]
}

export type ChatResponse = {
  kind: "data_query" | "schema_info" | "explanation" | "out_of_scope"
  answer: string
  sql: string | null
  data: ChatData | null
}

export async function sendChatMessage(
  database: string,
  message: string
): Promise<ChatResponse> {
  return api<ChatResponse>("/chat", {
    method: "POST",
    json: { database, message },
  })
}
