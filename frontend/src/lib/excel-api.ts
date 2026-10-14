import { api } from "@/lib/api"

/**
 * Client for Excel template reports.
 *
 * Its own module rather than an addition to lib/api.ts: this is a distinct
 * feature with its own lifecycle (scan → approve → plan → save → run), and
 * keeping it separate means nothing here can disturb the existing surfaces.
 */

export type SheetRole = "data" | "formula" | "parameter" | string

export type ScannedSheet = {
  name: string
  role: SheetRole
  header_row: number
  formula_count: number
  value_count: number
  formula_ratio: number
  header?: Record<string, string>
}

/** How many formulas fall into each regeneration tier. */
export type ScanSummary = {
  sheets: number
  tier_1: number
  tier_1b_blocks: number
  tier_1b_cells: number
  tier_2: number
  /** The only number that needs a human — formulas we will NOT rewrite. */
  tier_2_needs_review: number
  tier_3: number
}

export type Tier2Formula = {
  sheet: string
  cell: string
  formula: string
  reason?: string
  needs_review?: boolean
}

export type ScanResult = {
  scan_id: string
  template_name: string
  fingerprint?: string
  sheets: ScannedSheet[]
  summary: ScanSummary
  tab_roles?: Record<string, string>
  header_by_col?: Record<string, Record<string, string>>
  formula_classification?: {
    tier_1?: unknown[]
    tier_1b?: unknown[]
    tier_2?: Tier2Formula[]
    tier_3?: unknown[]
  }
  /** Set when this workbook matches a template that was already approved. */
  existing_template_id?: string | null
}

export type ApprovedTemplate = {
  template_id: string
  template_name: string
  data_sheet?: string
}

export type ReportParameter = {
  name: string
  type: string
  default?: unknown
  label?: string
}

/** One generated workbook. Every run is kept: re-running last quarter's report
 *  today gives different numbers for the same date, so the stored file is the
 *  only record of what was actually sent. */
export type ReportRun = {
  filename: string
  generated_at: string | null
}

export type SavedExcelReport = {
  report_id: string
  report_title: string
  description?: string | null
  template_id?: string
  template_name?: string | null
  /** "multiblock" for reports built in the block binder; "single_sheet" for the
   *  original one-sheet/one-query reports, which predate the `kind` marker. */
  kind?: string
  /** Block keys a multi-block report fills, e.g. ["Raw_Sales!1"]. */
  areas?: string[]
  data_sheet?: string
  parameters?: ReportParameter[]
  run_count?: number
  last_run_at?: string | null
  /** The most recent generated files, newest first — trimmed for display, so
   *  it can be shorter than `runs_total`. Only on the detail fetch. */
  runs?: ReportRun[]
  /** How many runs exist in total, including ones not listed. */
  runs_total?: number
  latest_output?: ReportRun | null
}

export type RunResult = {
  report_id: string
  report_title?: string
  row_count?: number
  output_filename?: string
  truncated?: boolean
  [key: string]: unknown
}

// ── Templates ───────────────────────────────────────────────────────────────

export async function scanWorkbook(file: File): Promise<ScanResult> {
  const form = new FormData()
  form.append("file", file)
  // No Content-Type header: the browser must set the multipart boundary.
  return api<ScanResult>("/excel/templates/scan", {
    method: "POST",
    body: form,
  })
}

export async function approveTemplate(
  scanId: string
): Promise<ApprovedTemplate> {
  return api<ApprovedTemplate>(
    `/excel/templates/${encodeURIComponent(scanId)}/approve`,
    { method: "POST" }
  )
}

// ── Reports ─────────────────────────────────────────────────────────────────

export type PlanRequest = {
  template_id: string
  report_title: string
  sql_template: string
  parameters?: ReportParameter[]
  data_sheet?: string
  description?: string
}

export type PlanResult = {
  plan_id: string
  report_title?: string
  data_sheet?: string
  parameters?: ReportParameter[]
  [key: string]: unknown
}

export async function planReport(body: PlanRequest): Promise<PlanResult> {
  return api<PlanResult>("/excel/reports/plan", { method: "POST", json: body })
}

export async function saveReport(planId: string): Promise<SavedExcelReport> {
  return api<SavedExcelReport>(
    `/excel/reports/${encodeURIComponent(planId)}/save`,
    { method: "POST" }
  )
}

export async function fetchExcelReports(): Promise<SavedExcelReport[]> {
  const res = await api<{ count: number; reports: SavedExcelReport[] }>(
    "/excel/reports"
  )
  return res.reports ?? []
}

export async function fetchExcelReport(id: string): Promise<SavedExcelReport> {
  return api<SavedExcelReport>(`/excel/reports/${encodeURIComponent(id)}`)
}

export async function runExcelReport(
  id: string,
  parameterValues: Record<string, unknown>
): Promise<RunResult> {
  return api<RunResult>(`/excel/reports/${encodeURIComponent(id)}/run`, {
    method: "POST",
    json: parameterValues,
  })
}

/** The generated workbook is a file download, not JSON — so this bypasses api().
 *  Without `run` it serves the newest; with one, that specific past run. */
export function downloadUrl(id: string, run?: string): string {
  const base = `/api/v1/excel/reports/${encodeURIComponent(id)}/download`
  return run ? `${base}?run=${encodeURIComponent(run)}` : base
}

/** Retire a saved report. Administrators only — the server enforces it. */
export async function deleteExcelReport(id: string): Promise<void> {
  await api<unknown>(`/excel/reports/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })
}

export function formatApiError(err: unknown): string {
  if (!(err instanceof Error)) return "Something went wrong."
  try {
    const parsed = JSON.parse(err.message) as { detail?: string }
    if (typeof parsed.detail === "string") return parsed.detail
  } catch {
    // Not JSON — fall through to the raw message.
  }
  return err.message || "Something went wrong."
}

// ── Multi-block reports ─────────────────────────────────────────────────────
//
// A template is not one sheet and one query. CreditFund_RiskDashboard takes
// three queries across two sheets, and its other two sheets must receive no
// data at all because the workbook computes them from the first two.

export type BlockToBind = {
  key: string
  sheet: string
  title: string | null
  first_row: number
  last_row: number
  rows: number
  spare_rows: number
  columns_needed: string[]
  columns_computed_by_template: string[]
}

export type PlannedSheet = {
  sheet: string
  role: "source" | "computed" | "parameter" | string
  reason: string
  overridden?: boolean
  blocks: number
}

export type UnboundArea = {
  key: string
  sheet: string
  title: string | null
  first_row: number
  last_row: number
  rows: number
  why: string
}

export type BlockPlan = {
  sheets: PlannedSheet[]
  blocks_to_bind: BlockToBind[]
  unbound_areas: UnboundArea[]
  summary: Record<string, number>
  warnings: string[]
}

export type MultiBlockRun = {
  storage_key: string
  output_filename: string
  template_id: string
  scan_id: string
  download_id: string
  report_title: string
  row_counts: Record<string, number>
  cells_moved: number
  blocks_filled: Array<{
    sheet: string
    first_row: number
    last_row: number
    rows_written: number
    filled_down: string[]
  }>
  blocks_regenerated: Array<{ sheet: string; first_row: number; rows: number }>
  column_warnings: string[]
}

/** Same, for an upload that has not been approved yet — configuration has to
 *  come before approval, not after. */
export async function fetchScanBlockPlan(
  scanId: string,
  roles: Record<string, string> = {},
  areas: Record<string, boolean> = {}
): Promise<BlockPlan> {
  return api<BlockPlan>(`/excel/scans/${encodeURIComponent(scanId)}/blocks`, {
    method: "POST",
    json: { roles, areas },
  })
}

/** What a report over this template needs. Read-only — runs no SQL. */
export async function fetchBlockPlan(templateId: string): Promise<BlockPlan> {
  return api<BlockPlan>(
    `/excel/templates/${encodeURIComponent(templateId)}/blocks`,
    { method: "POST" }
  )
}

export async function runMultiBlock(body: {
  template_id?: string
  scan_id?: string
  report_title?: string
  block_sql: Record<string, string>
  parameters?: ReportParameter[]
  parameter_values?: Record<string, unknown>
  allow_unmatched_columns?: boolean
  role_overrides?: Record<string, string>
  block_overrides?: Record<string, boolean>
}): Promise<MultiBlockRun> {
  return api<MultiBlockRun>("/excel/multiblock/run", {
    method: "POST",
    json: body,
  })
}

export function multiBlockDownloadUrl(templateId: string): string {
  return `/api/v1/excel/templates/${encodeURIComponent(templateId)}/download`
}

export type MultiBlockConfig = {
  block_sql: Record<string, string>
  parameters?: ReportParameter[]
  allow_unmatched_columns?: boolean
  role_overrides?: Record<string, string>
  block_overrides?: Record<string, boolean>
}

export type PublishedReport = {
  template_id: string
  template_name?: string
  report_id: string
  report_title: string
}

/**
 * Approve the workbook AND save its binding as a runnable report.
 *
 * The builder used to call approveTemplate() alone, which stored a workbook and
 * nothing else — no queries, no parameters, no row in the reports table. You
 * got your download and the Reports page stayed empty. This saves both halves,
 * so the report is something you can come back to next month.
 */
export async function publishMultiBlock(
  scanId: string,
  body: MultiBlockConfig & { report_title: string; description?: string }
): Promise<PublishedReport> {
  return api<PublishedReport>(
    `/excel/scans/${encodeURIComponent(scanId)}/publish`,
    { method: "POST", json: body }
  )
}
