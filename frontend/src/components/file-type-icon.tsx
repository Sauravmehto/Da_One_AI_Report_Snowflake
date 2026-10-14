/**
 * The icon and colour for a file, from its extension.
 *
 * One place rather than per list: a folder shows its workbooks, its documents
 * and its upload summary in three different components, and a PDF that is red
 * in one of them and grey in the others reads as two different things.
 *
 * Colour carries the meaning here, not shape — lucide has no distinct glyph for
 * a spreadsheet versus a PDF, so the tint is what makes a list scannable.
 *
 * Drawn as outlines in their kind's colour. Filling them was tried and is
 * worse: at 16px lucide's stroke is most of a small shape, so a filled page
 * with carved-out grid lines is a smudge rather than a spreadsheet.
 */
import {
  FileIcon,
  FileSpreadsheetIcon,
  FileTextIcon,
  FileTypeIcon as PdfGlyph,
  FolderIcon,
  FolderOpenIcon,
} from "lucide-react"

import { OUTLINE, SOLID } from "@/lib/accents"

type Kind = {
  Icon: typeof FileIcon
  className: string
  label: string
}

const SPREADSHEET: Kind = {
  Icon: FileSpreadsheetIcon,
  className: "text-emerald-600 dark:text-emerald-500",
  label: "Spreadsheet",
}

const WORD: Kind = {
  Icon: FileTextIcon,
  className: "text-blue-600 dark:text-blue-500",
  label: "Word document",
}

const PLAIN: Kind = {
  Icon: FileTextIcon,
  className: "text-muted-foreground",
  label: "Notes",
}

const KINDS: Record<string, Kind> = {
  xlsx: SPREADSHEET,
  xlsm: SPREADSHEET,
  xltx: SPREADSHEET,
  xltm: SPREADSHEET,
  xls: SPREADSHEET,
  csv: SPREADSHEET,
  pdf: {
    Icon: PdfGlyph,
    className: "text-red-600 dark:text-red-500",
    label: "PDF",
  },
  docx: WORD,
  doc: WORD,
  txt: PLAIN,
  md: PLAIN,
}

const UNKNOWN: Kind = {
  Icon: FileIcon,
  className: "text-muted-foreground",
  label: "File",
}

export function fileKind(filename: string): Kind {
  const dot = filename.lastIndexOf(".")
  if (dot === -1) return UNKNOWN
  return KINDS[filename.slice(dot + 1).toLowerCase()] ?? UNKNOWN
}

/** The icon for one file, solid in its kind's colour. */
export function FileTypeIcon({
  filename,
  className = "size-4",
}: {
  filename: string
  className?: string
}) {
  const kind = fileKind(filename)
  const Glyph = kind.Icon
  return (
    <Glyph
      className={`${className} shrink-0 ${kind.className}`}
      {...OUTLINE}
      role="img"
      aria-label={kind.label}
    />
  )
}

/** A folder, solid amber — the container the files sit inside. */
export function FolderGlyph({
  open = false,
  className = "size-4",
}: {
  open?: boolean
  className?: string
}) {
  const Glyph = open ? FolderOpenIcon : FolderIcon
  return (
    <Glyph
      className={`${className} shrink-0 text-amber-500`}
      {...SOLID}
      role="img"
      aria-label={open ? "Open folder" : "Folder"}
    />
  )
}
