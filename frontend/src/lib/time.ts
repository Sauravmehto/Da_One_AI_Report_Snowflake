/**
 * A time the server stored in UTC, as a Date.
 *
 * Dashboard and Excel-source times are TIMESTAMP_NTZ columns holding UTC, and
 * they reach the browser with no zone: "2026-09-13T04:06:27". `new Date()`
 * reads a zoneless date-time as LOCAL, so a viewer in the Gulf saw an 8:06
 * edit as 4:06 -- four hours early. Anything that already names its zone (a
 * trailing Z or an offset) is left exactly as it is.
 */
export function parseUtcTimestamp(value: string): Date {
  const text = value.trim()
  const dateTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text)
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)
  return new Date(dateTime && !zoned ? `${text.replace(" ", "T")}Z` : text)
}
