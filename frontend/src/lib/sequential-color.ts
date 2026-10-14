const SEQUENTIAL_SCALE = [
  "var(--chart-5)",
  "var(--chart-4)",
  "var(--chart-3)",
  "var(--chart-2)",
  "var(--chart-1)",
] as const

export function colorForSequentialValue(
  value: number,
  min: number,
  max: number
): string {
  if (Number.isNaN(value)) {
    return SEQUENTIAL_SCALE[0]
  }
  if (min === max) {
    return SEQUENTIAL_SCALE[SEQUENTIAL_SCALE.length - 1]
  }
  const t = (value - min) / (max - min)
  const idx = Math.round(t * (SEQUENTIAL_SCALE.length - 1))
  return SEQUENTIAL_SCALE[Math.min(Math.max(idx, 0), SEQUENTIAL_SCALE.length - 1)]
}

export function sequentialColorsForData(
  data: Record<string, string | number>[],
  valueKey: string
): string[] {
  const values = data
    .map((row) => Number(row[valueKey]))
    .filter((v) => !Number.isNaN(v))

  if (values.length === 0) {
    return data.map(() => SEQUENTIAL_SCALE[SEQUENTIAL_SCALE.length - 1])
  }

  const min = Math.min(...values)
  const max = Math.max(...values)

  return data.map((row) =>
    colorForSequentialValue(Number(row[valueKey]), min, max)
  )
}
