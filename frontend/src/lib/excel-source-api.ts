/**
 * Spreadsheets as a data source — upload folders of workbooks, ask questions.
 *
 * Separate from api.ts because uploads are multipart rather than JSON: the
 * shared `api()` helper sets a JSON content type and stringifies the body, and
 * a FormData POST must let the browser set its own multipart boundary.
 *
 * The unit here is a DATASET, keyed by workbook shape — twelve monthly rent
 * rolls are one dataset with twelve periods, not twelve uploads. That is what
 * makes a trend question possible.
 */

const API_BASE = "/api/v1"

/**
 * How a dataset is named in the `database` slot chat already carries.
 *
 * Reusing that slot is what lets a spreadsheet thread work with no new
 * plumbing — the conversation, its history and its resume path all carry a
 * source name already, and this is just another kind of name.
 */
const SPREADSHEET_PREFIX = "EXCEL:"
/** A whole folder: its tables and its documents at once. */
const COLLECTION_PREFIX = "FOLDER:"

export function sourceDatabaseName(datasetId: string): string {
  return `${SPREADSHEET_PREFIX}${datasetId}`
}

export function isSpreadsheetName(name: string): boolean {
  return name.startsWith(SPREADSHEET_PREFIX)
}

export function sourceIdFromName(name: string): string {
  return name.slice(SPREADSHEET_PREFIX.length)
}

export function collectionDatabaseName(collectionId: string): string {
  return `${COLLECTION_PREFIX}${collectionId}`
}

export function isCollectionName(name: string): boolean {
  return name.startsWith(COLLECTION_PREFIX)
}

export type Progress = {
  /** 0-100, driven by work actually completed on the server, not a timer. */
  percent: number
  stage: string
}

/** One table loaded out of a dataset's workbooks. */
export type SourceTable = {
  name: string
  row_count: number
  columns: string[]
  sheets: string[]
  grain: string
  purpose: string
  uncalculated_columns: string[]
  unreadable_columns: string[]
  /** Columns of the companion `<name>_derived` table, if it has one. */
  derived_columns?: string[]
}

/** One workbook that has been loaded into a dataset. */
export type DatasetFile = {
  filename: string
  as_of: string | null
  /** "sheet" | "filename" | "" — weaker evidence is worth showing. */
  as_of_source: string
  row_count: number
  uploaded_by: string
  uploaded_at: string
  /** The folder this file was picked from, if any. */
  folder?: string
}

export type Dataset = {
  dataset_id: string
  name: string
  fingerprint: string
  tables: SourceTable[]
  files: DatasetFile[]
  sheet_count: number
  created_at: string
  updated_at: string
  total_rows: number
  file_count: number
  period_start: string | null
  period_end: string | null
  warnings: string[]
}

/** The listing shape — lighter than a full dataset, for the library. */
export type DatasetSummary = {
  dataset_id: string
  name: string
  /** Set when every file came from one folder, so the library can group. */
  folder?: string
  file_count: number
  table_count: number
  total_rows: number
  period_start: string | null
  period_end: string | null
  updated_at: string
}

/** What happened to one file of an upload. */
export type FileOutcome = {
  filename: string
  status: "loaded" | "skipped" | "failed"
  dataset_id: string
  dataset_name: string
  as_of: string | null
  as_of_source: string
  row_count: number
  detail: string
}

export type BatchResult = {
  datasets: Dataset[]
  files: FileOutcome[]
  warnings: string[]
  /** Loaded, but with no period — answerable, just not trendable. */
  undated: FileOutcome[]
  /** Lock files, .DS_Store, unreadable formats. Noise, not errors. */
  skipped?: string[]
  /** PDFs, Word files and notes that arrived in the same upload. */
  documents?: DocumentInfo[]
  /** The folder everything landed in. */
  collection_id?: string
}

/**
 * A folder, holding whatever the upload turned into.
 *
 * The unit the library shows. One folder of same-shaped files holds a single
 * dataset; a mixed folder holds several. The name belongs to the user.
 */
/** One document in a folder. */
export type DocumentInfo = {
  source_file: string
  file_format: string
  doc_type: string
  page_count: number
  chunk_count: number
  fact_count: number
  is_scanned: boolean
}

export type Collection = {
  collection_id: string
  name: string
  /** False while it is grouped on the fly. Renaming works either way. */
  is_named: boolean
  file_count: number
  total_rows: number
  period_start: string | null
  period_end: string | null
  updated_at: string
  datasets: DatasetSummary[]
  documents?: DocumentInfo[]
}

export async function listCollections(): Promise<Collection[]> {
  const response = await fetch(`${API_BASE}/excel-sources`, {
    credentials: "include",
  })
  if (!response.ok) return []
  return (await response.json()) as Collection[]
}

/** Rename a folder. The datasets inside keep their own names. */
export async function renameCollection(
  collectionId: string,
  name: string
): Promise<Collection> {
  const response = await fetch(
    `${API_BASE}/excel-sources/collections/${encodeURIComponent(collectionId)}`,
    {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }
  )
  if (!response.ok) throw new Error(await response.text())
  return (await response.json()) as Collection
}

/** Remove a folder and everything in it: its datasets and its documents. */
export async function deleteCollection(collectionId: string): Promise<void> {
  const response = await fetch(
    `${API_BASE}/excel-sources/collections/${encodeURIComponent(collectionId)}`,
    { method: "DELETE", credentials: "include" }
  )
  if (!response.ok) throw new Error(await response.text())
}

/** Take one document back out of a folder. */
export async function removeCollectionDocument(
  collectionId: string,
  filename: string
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/excel-sources/collections/${encodeURIComponent(collectionId)}/documents/${encodeURIComponent(filename)}`,
    { method: "DELETE", credentials: "include" }
  )
  if (!response.ok) throw new Error(await response.text())
}

/** One dataset with its tables and files. Null if it is gone. */
export async function fetchDataset(datasetId: string): Promise<Dataset | null> {
  const response = await fetch(
    `${API_BASE}/excel-sources/${encodeURIComponent(datasetId)}`,
    { credentials: "include" }
  )
  if (!response.ok) return null
  return (await response.json()) as Dataset
}

/** Retire a dataset and every file in it. */
export async function deleteDataset(datasetId: string): Promise<void> {
  const response = await fetch(
    `${API_BASE}/excel-sources/${encodeURIComponent(datasetId)}`,
    { method: "DELETE", credentials: "include" }
  )
  if (!response.ok) {
    // useDeleteTarget parses a JSON body out of the message, so pass it through.
    throw new Error(await response.text())
  }
}

/** What removing one file left behind. */
export type FileRemoval = {
  /** True when that was the last file and the dataset went with it. */
  dataset_deleted?: boolean
  dataset?: Dataset
}

/**
 * Take one workbook back out of a dataset, leaving the others in place.
 *
 * Used for a file uploaded by mistake: the rows are deleted by their
 * `source_file`, so nothing else is re-read or re-mapped.
 */
export async function removeFile(
  datasetId: string,
  filename: string
): Promise<FileRemoval> {
  const response = await fetch(
    `${API_BASE}/excel-sources/${encodeURIComponent(datasetId)}/files/${encodeURIComponent(filename)}`,
    { method: "DELETE", credentials: "include" }
  )
  if (!response.ok) throw new Error(await response.text())
  const body = (await response.json()) as Dataset & { dataset_deleted?: boolean }
  if (body.dataset_deleted) return { dataset_deleted: true }
  return { dataset: body }
}

/**
 * Upload one or many workbooks — typically a whole folder — and stream the
 * ingestion.
 *
 * Each upload becomes its own folder, one dataset per shape found in it, so a
 * folder of twelve monthly rent rolls costs ONE model call and lands as one
 * dataset. Pass `addToCollectionId` to add these files to a folder that
 * already exists: matching shapes join what is there, new shapes become new
 * datasets inside it, and documents join its graph.
 */
export async function uploadFiles(
  files: File[],
  onProgress?: (progress: Progress) => void,
  addToCollectionId?: string
): Promise<BatchResult> {
  const body = new FormData()
  for (const file of files) {
    // webkitRelativePath keeps the folder the user picked, which is what names
    // the dataset.
    body.append("files", file, file.webkitRelativePath || file.name)
  }
  if (addToCollectionId) body.append("collection_id", addToCollectionId)

  const response = await fetch(`${API_BASE}/excel-sources/upload`, {
    method: "POST",
    credentials: "include",
    body,
  })

  if (!response.ok) {
    const text = await response.text()
    let message = text
    try {
      const parsed = JSON.parse(text) as { detail?: string }
      if (parsed.detail) message = parsed.detail
    } catch {
      /* not JSON — use the raw text */
    }
    throw new Error(message || `Upload failed with status ${response.status}`)
  }
  if (!response.body) throw new Error("The server returned an empty response.")

  const result = await consumeStream<BatchResult>(response.body, onProgress)
  if (!result) throw new Error("The upload ended without returning a result.")
  return result
}

/** Read an SSE stream of progress/complete/failed frames. */
async function consumeStream<T>(
  body: ReadableStream<Uint8Array>,
  onProgress?: (progress: Progress) => void
): Promise<T | null> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  let result: T | null = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let split = buffer.indexOf("\n\n")
    while (split !== -1) {
      const frame = buffer.slice(0, split)
      buffer = buffer.slice(split + 2)

      let event = "message"
      const dataLines: string[] = []
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim()
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim())
      }

      if (dataLines.length > 0) {
        const data = JSON.parse(dataLines.join("\n"))
        if (event === "progress") onProgress?.(data as Progress)
        else if (event === "complete") result = data as T
        else if (event === "failed") throw new Error(data.detail as string)
      }

      split = buffer.indexOf("\n\n")
    }
  }
  return result
}
