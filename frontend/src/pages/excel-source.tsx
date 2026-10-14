/**
 * Files — the folders a firm uploads, holding spreadsheets and documents.
 *
 * Kept out of the chat database picker: a firm has a dozen warehouses but
 * accumulates hundreds of files, and one list cannot serve both.
 *
 * A row is a FOLDER. Its workbooks become datasets keyed by shape, its PDFs
 * and notes become a searchable graph, and "Ask this folder" puts both in
 * scope at once — which is the only way a question spanning them is
 * answerable. There is no configuration step: anything ambiguous is raised in
 * chat, where it would change an answer.
 */

import * as React from "react"
import { useNavigate } from "react-router-dom"
import {
  ArrowRightIcon,
  CalendarRangeIcon,
  ChevronRightIcon,
  FileSpreadsheetIcon,
  FolderUpIcon,
  CheckIcon,
  FileTextIcon,
  Loader2Icon,
  PencilIcon,
  PlusIcon,
  SearchIcon,
  Trash2Icon,
  UploadIcon,
  XIcon,
} from "lucide-react"
import { toast } from "sonner"

import { DeleteDialog } from "@/components/delete-dialog"
import { FileTypeIcon, FolderGlyph } from "@/components/file-type-icon"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useDeleteTarget } from "@/hooks/use-delete-target"
import {
  deleteCollection,
  deleteDataset,
  fetchDataset,
  collectionDatabaseName,
  listCollections,
  removeCollectionDocument,
  removeFile,
  renameCollection,
  sourceDatabaseName,
  uploadFiles,
  type BatchResult,
  type Collection,
  type Dataset,
  type DocumentInfo,
  type DatasetSummary,
  type Progress,
} from "@/lib/excel-source-api"

//: Everything the server will read. Kept in step with SUPPORTED_SUFFIXES on
//: the upload route: an extension missing here cannot even be picked, and
//: the file dialog simply greys the file out with no explanation.
const ACCEPT = ".xlsx,.xlsm,.xltx,.xltm,.xls,.pdf,.docx,.txt,.md"

function toWords(value: string): string {
  const words = value.replace(/_/g, " ").trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** "Jan 2026 – Dec 2026", or a single month when there is only one. */
function periodRange(start: string | null, end: string | null): string | null {
  if (!start) return null
  const format = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(undefined, {
      month: "short",
      year: "numeric",
    })
  const from = format(start)
  const to = end ? format(end) : from
  return from === to ? from : `${from} – ${to}`
}

/** One dataset in the library, expandable to show its tables and files. */
function DatasetRow({
  summary,
  reloadKey,
  standsForCollection,
  onRenamed,
  onAsk,
  onAskFolder,
  onAddFiles,
  onRemoveFile,
  onDelete,
}: {
  summary: DatasetSummary
  /** Bumped when a file is removed, so the open detail refetches. */
  reloadKey: number
  /** Set when this is the only dataset in its folder, so the row IS the folder:
      it takes the folder's name and the folder's rename control rather than
      making the user open a container holding one thing. */
  standsForCollection?: Collection
  onRenamed?: () => void
  onAsk: (datasetId: string) => void
  onAskFolder: (collectionId: string) => void
  onAddFiles: (collectionId: string) => void
  onRemoveFile: (summary: DatasetSummary, filename: string) => void
  onDelete: (summary: DatasetSummary) => void
}) {
  const [detail, setDetail] = React.useState<Dataset | null>(null)
  const [open, setOpen] = React.useState(false)

  async function toggle() {
    const next = !open
    setOpen(next)
    // Fetched only when opened: the listing deliberately carries counts rather
    // than every table and file, so a library of hundreds stays one request.
    if (next && !detail) setDetail(await fetchDataset(summary.dataset_id))
  }

  React.useEffect(() => {
    if (!open || reloadKey === 0) return
    void fetchDataset(summary.dataset_id).then(setDetail)
  }, [reloadKey, open, summary.dataset_id])

  const period = periodRange(summary.period_start, summary.period_end)

  return (
    <div className="group rounded-lg border bg-background">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3">
        <button
          type="button"
          onClick={() => void toggle()}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <ChevronRightIcon
            className={
              "size-4 shrink-0 text-muted-foreground transition-transform " +
              (open ? "rotate-90" : "")
            }
          />
          {standsForCollection ? (
            <FolderGlyph open={open} className="size-[18px]" />
          ) : (
            <FileSpreadsheetIcon
              className="size-4 shrink-0 text-emerald-600 dark:text-emerald-500"
              fill="currentColor"
              fillOpacity={0.18}
              strokeWidth={1.75}
            />
          )}
          <span className="min-w-0">
            {standsForCollection ? (
              <FolderName
                collection={standsForCollection}
                onRenamed={onRenamed ?? (() => {})}
              />
            ) : (
              <span className="block truncate font-medium">{summary.name}</span>
            )}
            <span className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
              <span>
                {summary.file_count} file{summary.file_count === 1 ? "" : "s"}
              </span>
              <span>· {summary.total_rows.toLocaleString()} rows</span>
              <span>
                · {summary.table_count} table
                {summary.table_count === 1 ? "" : "s"}
              </span>
              {period ? (
                <span className="flex items-center gap-1">
                  ·<CalendarRangeIcon className="size-3" />
                  {period}
                </span>
              ) : null}
            </span>
          </span>
        </button>

        {/* Only when this row IS its folder. Inside a folder the datasets are
            files grouped by shape, not containers of their own — adding to one
            of them would ask the user to decide which shape their new file is,
            which is the loader's job. */}
        {standsForCollection ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onAddFiles(standsForCollection.collection_id)}
            className="gap-1.5"
            title={`Add more files to ${standsForCollection.name}`}
          >
            <PlusIcon className="size-3.5" />
            Add files
          </Button>
        ) : null}

        {/* When this row IS its folder, Ask has to bind the FOLDER: binding
            the dataset alone would leave any PDFs in the same folder invisible
            to the conversation. Inside a folder it binds just this dataset,
            which is the point of asking one of several. */}
        <Button
          size="sm"
          onClick={() =>
            standsForCollection
              ? onAskFolder(standsForCollection.collection_id)
              : onAsk(summary.dataset_id)
          }
          className="gap-1.5"
        >
          Ask
          <ArrowRightIcon className="size-3.5" />
        </Button>

        {/* Quiet until you look for it: a destructive control on every row of a
            list competes with the thing the list is actually for. */}
        <button
          type="button"
          onClick={() => onDelete(summary)}
          aria-label={`Delete ${summary.name}`}
          title={`Delete ${summary.name}`}
          className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          <Trash2Icon className="size-4" />
        </button>
      </div>

      {open ? (
        <div className="space-y-3 border-t px-3 py-3">
          {detail ? (
            <>
              <div className="space-y-1.5">
                {detail.tables.map((table) => (
                  <div key={table.name} className="text-sm">
                    <span className="font-medium">{toWords(table.name)}</span>
                    <span className="ml-2 text-muted-foreground">
                      {table.row_count.toLocaleString()} rows
                    </span>
                    {table.grain ? (
                      <p className="text-xs text-muted-foreground">
                        {table.grain}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>

              <div>
                <p className="text-xs font-medium text-muted-foreground">
                  Files
                </p>
                <div className="mt-1 overflow-x-auto">
                  <table className="w-full min-w-[380px] text-xs">
                    <tbody>
                      {detail.files.map((file) => (
                        <tr key={file.filename} className="border-b last:border-0">
                          <td className="py-1 pr-3">
                            <span className="flex items-center gap-1.5">
                              <FileTypeIcon filename={file.filename} />
                              {file.filename}
                            </span>
                          </td>
                          <td className="py-1 pr-3 text-muted-foreground">
                            {file.as_of ?? "no period"}
                          </td>
                          <td className="py-1 text-muted-foreground">
                            {/* A period read off the file name is weaker
                                evidence than one the sheet states. */}
                            {file.as_of_source === "filename"
                              ? "from file name"
                              : ""}
                          </td>
                          <td className="w-8 py-1 text-right">
                            <button
                              type="button"
                              onClick={() => onRemoveFile(summary, file.filename)}
                              aria-label={`Remove ${file.filename}`}
                              title={`Remove ${file.filename} from this dataset`}
                              className="rounded p-1 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                            >
                              <XIcon className="size-3.5" />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          ) : (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2Icon className="size-3.5 animate-spin" />
              Loading…
            </p>
          )}
        </div>
      ) : null}
    </div>
  )
}

/** The PDFs, Word files and notes in a folder.

    Listed apart from the datasets because they answer differently: a table is
    queried, a document is searched. Both are asked through the same folder. */
function DocumentList({
  documents,
  onRemove,
}: {
  documents: DocumentInfo[]
  onRemove: (filename: string) => void
}) {
  if (documents.length === 0) return null
  return (
    <div className="rounded-lg border bg-background p-3">
      <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <FileTextIcon className="size-3.5" strokeWidth={1.75} />
        {documents.length} document{documents.length === 1 ? "" : "s"}
      </p>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[380px] text-xs">
          <tbody>
            {documents.map((doc) => (
              <tr key={doc.source_file} className="border-b last:border-0">
                <td className="py-1 pr-3">
                  <span className="flex items-center gap-1.5">
                    <FileTypeIcon filename={doc.source_file} />
                    {doc.source_file}
                  </span>
                </td>
                <td className="py-1 pr-3 text-muted-foreground">
                  {doc.doc_type ? toWords(doc.doc_type) : ""}
                </td>
                <td className="py-1 pr-3 text-muted-foreground">
                  {doc.page_count} page{doc.page_count === 1 ? "" : "s"}
                </td>
                <td className="py-1 pr-3 text-muted-foreground">
                  {/* A scan holds no text, so it is in the folder but cannot
                      be searched or quoted. Saying so beats an empty result. */}
                  {doc.is_scanned
                    ? "scanned, no text"
                    : doc.fact_count
                      ? `${doc.fact_count} figures`
                      : ""}
                </td>
                <td className="w-8 py-1 text-right">
                  <button
                    type="button"
                    onClick={() => onRemove(doc.source_file)}
                    aria-label={`Remove ${doc.source_file}`}
                    title={`Remove ${doc.source_file} from this folder`}
                    className="rounded p-1 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                  >
                    <XIcon className="size-3.5" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}


/** A folder's name, editable in place.

    Folders are named from what was uploaded, which produces things like
    "files-different-schema". The name is the label a person uses to find their
    data again, so it has to be theirs to change. */
function FolderName({
  collection,
  onRenamed,
}: {
  collection: Collection
  onRenamed: () => void
}) {
  const [editing, setEditing] = React.useState(false)
  const [draft, setDraft] = React.useState(collection.name)
  const [busy, setBusy] = React.useState(false)

  async function save() {
    const next = draft.trim()
    if (!next || next === collection.name) {
      setEditing(false)
      setDraft(collection.name)
      return
    }
    setBusy(true)
    try {
      await renameCollection(collection.collection_id, next)
      onRenamed()
      setEditing(false)
    } catch {
      toast.error("Could not rename that folder")
      setDraft(collection.name)
    } finally {
      setBusy(false)
    }
  }

  if (!editing) {
    return (
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="truncate font-medium">{collection.name}</span>
        <button
          type="button"
          onClick={(event) => {
            // The name sits inside the row's expand button on a folder row.
            event.stopPropagation()
            setDraft(collection.name)
            setEditing(true)
          }}
          aria-label={`Rename ${collection.name}`}
          title="Rename"
          className="rounded p-0.5 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          <PencilIcon className="size-3" />
        </button>
      </span>
    )
  }

  return (
    <span
      className="flex min-w-0 items-center gap-1"
      onClick={(event) => event.stopPropagation()}
    >
      <Input
        autoFocus
        value={draft}
        disabled={busy}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") void save()
          if (event.key === "Escape") {
            setDraft(collection.name)
            setEditing(false)
          }
        }}
        onBlur={() => void save()}
        className="h-7 max-w-[260px] text-sm"
      />
      <button
        type="button"
        // onMouseDown, not onClick: the input's blur would fire first and the
        // button would be gone before the click landed.
        onMouseDown={(event) => {
          event.preventDefault()
          void save()
        }}
        aria-label="Save name"
        className="rounded p-1 text-muted-foreground hover:text-foreground"
      >
        {busy ? (
          <Loader2Icon className="size-3.5 animate-spin" />
        ) : (
          <CheckIcon className="size-3.5" />
        )}
      </button>
    </span>
  )
}

/** Several datasets that arrived in one folder, collapsed into one row.

    Closed by default: a folder is the unit somebody uploaded, and a library of
    a hundred is only navigable if each one starts as a single line. The heading
    carries enough to decide whether to open it. */
function FolderGroup({
  collection,
  forceOpen,
  onRenamed,
  onAskFolder,
  onAddFiles,
  onRemoveDocument,
  onDeleteFolder,
  children,
}: {
  collection: Collection
  /** Held open while a search is running, or matches inside stay hidden. */
  forceOpen: boolean
  onRenamed: () => void
  onAskFolder: (collectionId: string) => void
  onAddFiles: (collectionId: string) => void
  onRemoveDocument: (collection: Collection, filename: string) => void
  onDeleteFolder: (collection: Collection) => void
  children: React.ReactNode
}) {
  const [open, setOpen] = React.useState(false)
  const isOpen = forceOpen || open
  const period = periodRange(collection.period_start, collection.period_end)
  const documents = collection.documents ?? []

  return (
    <div className="group rounded-lg border">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={isOpen}
        className="flex min-w-0 flex-1 items-center gap-2 text-left"
      >
        <ChevronRightIcon
          className={
            "size-4 shrink-0 text-muted-foreground transition-transform " +
            (isOpen ? "rotate-90" : "")
          }
        />
        {/* Open and closed are worth distinguishing at a glance in a list of
            folders that are mostly closed. */}
        <FolderGlyph open={isOpen} className="size-[18px]" />
        <span className="min-w-0">
          <FolderName collection={collection} onRenamed={onRenamed} />
          <span className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
            <span>{collection.datasets.length} datasets</span>
            {documents.length > 0 ? (
              <span>
                · {documents.length} document{documents.length === 1 ? "" : "s"}
              </span>
            ) : null}
            <span>
              · {collection.file_count} file
              {collection.file_count === 1 ? "" : "s"}
            </span>
            <span>· {collection.total_rows.toLocaleString()} rows</span>
            {period ? (
              <span className="flex items-center gap-1">
                ·<CalendarRangeIcon className="size-3" />
                {period}
              </span>
            ) : null}
          </span>
        </span>
      </button>

      <Button
        size="sm"
        variant="ghost"
        onClick={() => onAddFiles(collection.collection_id)}
        className="gap-1.5"
        title={`Add more files to ${collection.name}`}
      >
        <PlusIcon className="size-3.5" />
        Add files
      </Button>

      {/* Asking the FOLDER, not one source in it: the agent gets every table
          and every document at once, which is the only way a question that
          spans them can be answered. */}
      <Button
        size="sm"
        onClick={() => onAskFolder(collection.collection_id)}
        className="gap-1.5"
        title={`Ask across everything in ${collection.name}`}
      >
        Ask this folder
        <ArrowRightIcon className="size-3.5" />
      </Button>

      {/* Deleting the folder, not a dataset in it. Without this a folder that
          fanned out into six datasets can only be cleared six times over. */}
      <button
        type="button"
        onClick={() => onDeleteFolder(collection)}
        aria-label={`Delete ${collection.name}`}
        title={`Delete ${collection.name} and everything in it`}
        className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <Trash2Icon className="size-4" />
      </button>
      </div>

      {isOpen ? (
        <div className="space-y-2 border-t bg-muted/30 p-2">
          {children}
          <DocumentList
            documents={documents}
            onRemove={(filename) => onRemoveDocument(collection, filename)}
          />
        </div>
      ) : null}
    </div>
  )
}

/** What a finished upload actually did, before anyone asks anything. */
function BatchSummary({
  batch,
  onAsk,
}: {
  batch: BatchResult
  onAsk: (datasetId: string) => void
}) {
  const failed = batch.files.filter((f) => f.status === "failed")

  return (
    <div className="mt-4 rounded-lg border p-4">
      <p className="text-sm font-medium">
        Loaded {batch.files.filter((f) => f.status === "loaded").length} files
        into {batch.datasets.length} dataset
        {batch.datasets.length === 1 ? "" : "s"}
      </p>

      <div className="mt-3 space-y-2">
        {batch.datasets.map((dataset) => {
          const period = periodRange(dataset.period_start, dataset.period_end)
          return (
            <div
              key={dataset.dataset_id}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm"
            >
              <span className="font-medium">{dataset.name}</span>
              <span className="text-muted-foreground">
                {dataset.file_count} files · {dataset.total_rows.toLocaleString()}{" "}
                rows{period ? ` · ${period}` : ""}
              </span>
              <Button
                size="sm"
                variant="outline"
                className="ml-auto gap-1.5"
                onClick={() => onAsk(dataset.dataset_id)}
              >
                Ask
                <ArrowRightIcon className="size-3.5" />
              </Button>
            </div>
          )
        })}
      </div>

      {batch.undated.length > 0 ? (
        <p className="mt-3 text-xs text-amber-600 dark:text-amber-500">
          {batch.undated.length} file
          {batch.undated.length === 1 ? " has" : "s have"} no period, so{" "}
          {batch.undated.length === 1 ? "it is" : "they are"} excluded from
          trends: {batch.undated.map((f) => f.filename).join(", ")}
        </p>
      ) : null}

      {failed.length > 0 ? (
        <p className="mt-2 text-xs text-destructive">
          {failed.length} could not be read:{" "}
          {failed.map((f) => f.filename).join(", ")}
        </p>
      ) : null}

      {batch.skipped && batch.skipped.length > 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Ignored {batch.skipped.length} non-spreadsheet file
          {batch.skipped.length === 1 ? "" : "s"}.
        </p>
      ) : null}

      {batch.warnings.map((warning, index) => (
        <p key={index} className="mt-2 text-xs text-muted-foreground">
          {warning}
        </p>
      ))}
    </div>
  )
}

export default function ExcelSourcePage() {
  const navigate = useNavigate()
  const [collections, setCollections] = React.useState<Collection[]>([])
  const [query, setQuery] = React.useState("")
  const [isLoading, setLoading] = React.useState(true)
  const [progress, setProgress] = React.useState<Progress | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [batch, setBatch] = React.useState<BatchResult | null>(null)
  const [dragging, setDragging] = React.useState(false)
  // Which FOLDER the next picked files should join, if any. One hidden input
  // serves every row, so the target is held here rather than on the input.
  const [addTo, setAddTo] = React.useState<string | null>(null)
  // Bumped after a removal so any open dataset detail refetches itself.
  const [reloadKey, setReloadKey] = React.useState(0)
  const fileRef = React.useRef<HTMLInputElement>(null)
  const folderRef = React.useRef<HTMLInputElement>(null)

  const load = React.useCallback(async () => {
    setLoading(true)
    setCollections(await listCollections())
    setLoading(false)
  }, [])

  React.useEffect(() => {
    void load()
  }, [load])

  // webkitdirectory is not in React's HTMLInputElement typings, and setting it
  // as a JSX prop is dropped. Applied to the node directly instead.
  React.useEffect(() => {
    if (folderRef.current) {
      folderRef.current.setAttribute("webkitdirectory", "")
      folderRef.current.setAttribute("directory", "")
    }
  }, [])

  const isBusy = progress !== null

  async function upload(chosen: FileList | File[] | null, addTo?: string) {
    const list = Array.from(chosen ?? [])
    if (list.length === 0) return

    setError(null)
    setBatch(null)
    setProgress({ percent: 0, stage: "Uploading" })
    try {
      const result = await uploadFiles(list, setProgress, addTo)
      setBatch(result)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed")
    } finally {
      setProgress(null)
      setAddTo(null)
    }
  }

  function pickFilesFor(collectionId: string) {
    setAddTo(collectionId)
    fileRef.current?.click()
  }

  const del = useDeleteTarget<{ id: string; name: string }>(
    deleteDataset,
    React.useCallback(() => {
      void load()
      toast.success("Dataset deleted")
    }, [load])
  )

  function ask(datasetId: string) {
    navigate("/", { state: { database: sourceDatabaseName(datasetId) } })
  }

  function askFolder(collectionId: string) {
    navigate("/", { state: { database: collectionDatabaseName(collectionId) } })
  }

  const documentRemoval = useDeleteTarget<{ id: string; name: string }>(
    React.useCallback(async (id: string) => {
      const cut = id.indexOf("/")
      await removeCollectionDocument(id.slice(0, cut), id.slice(cut + 1))
    }, []),
    React.useCallback(() => {
      void load()
      toast.success("Document removed")
    }, [load])
  )

  const folderRemoval = useDeleteTarget<{ id: string; name: string }>(
    deleteCollection,
    React.useCallback(() => {
      void load()
      toast.success("Folder deleted")
    }, [load])
  )

  function askRemoveDocument(collection: Collection, filename: string) {
    documentRemoval.ask({
      id: `${collection.collection_id}/${filename}`,
      name: filename,
    })
  }

  const removal = useDeleteTarget<{
    id: string
    name: string
  }>(
    // The dialog carries one id, so the pair travels as "<datasetId>/<file>".
    // A dataset id is hex and a leaf file name holds no slash, so the first
    // slash separates them unambiguously.
    React.useCallback(async (id: string) => {
      const cut = id.indexOf("/")
      await removeFile(id.slice(0, cut), id.slice(cut + 1))
    }, []),
    React.useCallback(() => {
      setReloadKey((n) => n + 1)
      void load()
      toast.success("File removed")
    }, [load])
  )

  function askRemoveFile(summary: DatasetSummary, filename: string) {
    removal.ask({
      id: `${summary.dataset_id}/${filename}`,
      name:
        summary.file_count === 1
          ? `${filename} — the only file in “${summary.name}”, so the dataset goes with it`
          : filename,
    })
  }

  const needle = query.trim().toLowerCase()
  // A folder matches by its own name or by any dataset in it; matching the
  // folder keeps all of its datasets, because that is the thing being searched
  // for. Only the folder's own datasets are narrowed otherwise.
  const filtered = needle
    ? collections.flatMap((collection) => {
        if (collection.name.toLowerCase().includes(needle)) return [collection]
        const datasets = collection.datasets.filter((d) =>
          d.name.toLowerCase().includes(needle)
        )
        return datasets.length ? [{ ...collection, datasets }] : []
      })
    : collections

  return (
    /* AppShell wraps the outlet in overflow-hidden at a fixed viewport height,
       so a page that does not bring its own scroll container has its overflow
       silently clipped. Same pattern as reports.tsx. */
    <div className="scrollbar-slim h-full min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl p-6">
        <header className="mb-5">
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <FolderGlyph className="size-5" />
            Files
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Spreadsheets, PDFs and documents you can ask questions of, the
            same way you would a database.
          </p>
        </header>

        <div
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault()
            setDragging(false)
            if (!isBusy) void upload(event.dataTransfer.files)
          }}
          className={
            "flex flex-col items-center justify-center rounded-lg border-2 border-dashed p-6 transition-colors " +
            (dragging ? "border-primary bg-accent/40" : "border-muted")
          }
        >
          {isBusy && progress ? (
            <div className="w-full max-w-md">
              <div className="flex items-center gap-2 text-sm">
                <Loader2Icon className="size-4 shrink-0 animate-spin motion-reduce:animate-none" />
                <span>Reading your files — {progress.percent}% complete</span>
              </div>
              <div
                className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted"
                role="progressbar"
                aria-valuenow={progress.percent}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Upload progress"
              >
                <div
                  className="h-full rounded-full bg-primary transition-[width] duration-300 ease-out motion-reduce:transition-none"
                  style={{ width: `${progress.percent}%` }}
                />
              </div>
              <p className="mt-1.5 text-xs text-muted-foreground">
                {progress.stage}
              </p>
            </div>
          ) : (
            <>
              <UploadIcon className="size-5 text-muted-foreground" />
              <p className="mt-2 text-sm">Drop files or a folder here</p>
              <div className="mt-3 flex flex-wrap justify-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1.5"
                  onClick={() => folderRef.current?.click()}
                >
                  <FolderUpIcon className="size-4" />
                  Choose a folder
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => fileRef.current?.click()}
                >
                  Choose files
                </Button>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                Spreadsheets (.xlsx, .xlsm, .xls) and documents (.pdf, .docx,
                .txt) — 25MB per workbook, 60MB per document
              </p>
            </>
          )}

          <input
            ref={fileRef}
            type="file"
            multiple
            accept={ACCEPT}
            className="hidden"
            onChange={(event) => {
              void upload(event.target.files, addTo ?? undefined)
              event.target.value = ""
            }}
          />
          {/* No `accept` on the folder input: a directory pick hands over
              everything inside it, and the server filters. Constraining it here
              causes some browsers to offer nothing at all. */}
          <input
            ref={folderRef}
            type="file"
            multiple
            className="hidden"
            onChange={(event) => {
              void upload(event.target.files)
              event.target.value = ""
            }}
          />
        </div>

        {error ? (
          <div className="mt-4 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
            <p className="font-medium">Could not read these spreadsheets</p>
            <p className="mt-1 text-muted-foreground">{error}</p>
          </div>
        ) : null}

        {batch ? <BatchSummary batch={batch} onAsk={ask} /> : null}

        <div className="mt-6 flex flex-wrap items-center gap-3">
          <h2 className="text-sm font-semibold">Folders</h2>
          <Badge variant="secondary">{collections.length}</Badge>
          {collections.length > 5 ? (
            <div className="relative ml-auto w-56">
              <SearchIcon className="absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search"
                className="h-8 pl-8 text-sm"
              />
            </div>
          ) : null}
        </div>

        <div className="mt-3 space-y-2">
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : filtered.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {collections.length === 0
                ? "Nothing uploaded yet. Drop a folder above to get started."
                : "Nothing matches that search."}
            </p>
          ) : (
            filtered.map((collection) => {
              const rows = collection.datasets.map((summary) => (
                <DatasetRow
                  key={summary.dataset_id}
                  summary={summary}
                  reloadKey={reloadKey}
                  standsForCollection={
                    collection.datasets.length === 1 ? collection : undefined
                  }
                  onRenamed={load}
                  onAsk={ask}
                  onAskFolder={askFolder}
                  onAddFiles={pickFilesFor}
                  onRemoveFile={askRemoveFile}
                  onDelete={(d) => del.ask({ id: d.dataset_id, name: d.name })}
                />
              ))
              // A folder holding one thing IS that thing: the row above already
              // carries its name and its rename control, so wrapping it in a
              // container would only add a click. Documents change that — with
              // them there are two kinds of source, and the folder is what asks
              // across both.
              const documents = collection.documents ?? []
              if (collection.datasets.length === 1 && documents.length === 0) {
                return rows
              }
              return (
                <FolderGroup
                  key={collection.collection_id}
                  collection={collection}
                  forceOpen={needle.length > 0}
                  onRenamed={load}
                  onAskFolder={askFolder}
                  onAddFiles={pickFilesFor}
                  onRemoveDocument={askRemoveDocument}
                  onDeleteFolder={(c) =>
                    folderRemoval.ask({
                      id: c.collection_id,
                      name: c.name,
                    })
                  }
                >
                  {rows}
                </FolderGroup>
              )
            })
          )}
        </div>
      </div>

      <DeleteDialog
        open={del.target !== null}
        name={del.target?.name ?? ""}
        kind="spreadsheet"
        busy={del.busy}
        error={del.error}
        onConfirm={del.confirm}
        onOpenChange={(next) => {
          if (!next) del.close()
        }}
      />

      <DeleteDialog
        open={folderRemoval.target !== null}
        name={folderRemoval.target?.name ?? ""}
        kind="folder"
        busy={folderRemoval.busy}
        error={folderRemoval.error}
        onConfirm={folderRemoval.confirm}
        onOpenChange={(next) => {
          if (!next) folderRemoval.close()
        }}
      />

      <DeleteDialog
        open={documentRemoval.target !== null}
        name={documentRemoval.target?.name ?? ""}
        kind="file"
        busy={documentRemoval.busy}
        error={documentRemoval.error}
        onConfirm={documentRemoval.confirm}
        onOpenChange={(next) => {
          if (!next) documentRemoval.close()
        }}
      />

      <DeleteDialog
        open={removal.target !== null}
        name={removal.target?.name ?? ""}
        kind="file"
        busy={removal.busy}
        error={removal.error}
        onConfirm={removal.confirm}
        onOpenChange={(next) => {
          if (!next) removal.close()
        }}
      />
    </div>
  )
}
