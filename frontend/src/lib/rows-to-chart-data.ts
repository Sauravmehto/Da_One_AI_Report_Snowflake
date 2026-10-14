import type { ChatData } from "@/lib/api"
import type { ChartEncoding } from "@/lib/report-types"

export function rowsToChartData(
  columns: string[],
  rows: ChatData["rows"],
  encoding: ChartEncoding
): Record<string, string | number | boolean | null>[] {
  const xIndex = columns.indexOf(encoding.x)
  if (xIndex === -1) {
    return []
  }

  const yIndices = encoding.y
    .map((key) => columns.indexOf(key))
    .filter((index) => index !== -1 && index !== xIndex)

  return rows.map((row) => {
    const point: Record<string, string | number | boolean | null> = {
      [encoding.x]: row[xIndex] ?? null,
    }
    encoding.y.forEach((key, i) => {
      const idx = yIndices[i]
      if (idx !== undefined) {
        point[key] = row[idx] ?? null
      }
    })
    return point
  })
}
