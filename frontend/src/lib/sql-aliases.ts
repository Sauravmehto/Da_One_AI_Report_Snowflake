/**
 * The columns a panel's SQL actually returns — for the autocomplete below and
 * the hint under the SQL box.
 *
 * Only the OUTERMOST select list counts. The previous version matched every
 * `AS` in the statement, and `AS` means three different things: an output
 * alias, an alias inside a WITH block, and a cast's target type. On a panel
 * that casts text columns inside a CTE — which every hospitality and retail
 * panel does — it announced FLOAT and the CTE's private names as columns you
 * could chart, and offered them in the dropdown. Half of each list was
 * fiction, and choosing one of the fictions produced an empty panel.
 *
 * Parsed by bracket depth rather than by a SQL grammar: a CTE body, a
 * subquery and a function call are all parenthesised, so only depth-zero text
 * belongs to the outermost statement. The FIRST depth-zero SELECT is the one
 * that names the result — in a UNION it is the first branch that decides the
 * column names, which is also where the planner writes its aliases.
 *
 * Deliberately silent rather than wrong: an unaliased expression (SUM(x)) and
 * a bare * are skipped, so the list may be short. An empty list falls back to
 * the generic sentence instead of naming something that does not exist.
 */
export function sqlAliases(sql: string | null | undefined): string[] {
  if (!sql) return []

  // Literals and comments can hold commas, brackets and the word FROM.
  const text = sql
    .replace(/--[^\n]*/g, " ")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/'(?:[^']|'')*'/g, "''")

  // Walk once, recording depth-zero SELECT / FROM / comma positions.
  let depth = 0
  let selectAt = -1
  let fromAt = -1
  const commas: number[] = []
  const word = /[A-Za-z_][A-Za-z0-9_]*/y
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i]
    if (ch === "(") depth += 1
    else if (ch === ")") depth = Math.max(0, depth - 1)
    else if (depth === 0 && ch === "," && selectAt >= 0 && fromAt < 0) commas.push(i)
    else if (depth === 0 && /[A-Za-z_]/.test(ch)) {
      word.lastIndex = i
      const match = word.exec(text)
      if (!match) continue
      const keyword = match[0].toUpperCase()
      i = word.lastIndex - 1
      if (keyword === "SELECT" && selectAt < 0) selectAt = match.index + match[0].length
      else if (keyword === "FROM" && selectAt >= 0 && fromAt < 0) fromAt = match.index
    }
  }
  if (selectAt < 0) return []

  const list = text.slice(selectAt, fromAt < 0 ? undefined : fromAt)
  const cuts = commas.map((c) => c - selectAt).filter((c) => c > 0 && c < list.length)
  const items: string[] = []
  let start = 0
  for (const cut of [...cuts, list.length]) {
    items.push(list.slice(start, cut))
    start = cut + 1
  }

  const names: string[] = []
  for (const raw of items) {
    const item = raw.replace(/^\s*DISTINCT\s+/i, "").trim()
    if (!item || item === "*") continue
    const aliased = item.match(/\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$/i)
    if (aliased) {
      names.push(aliased[1])
      continue
    }
    // A bare column, optionally qualified: t.AMOUNT -> AMOUNT. Anything else is
    // an expression whose name the warehouse invents; do not guess it.
    const bare = item.match(/^(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?([A-Za-z_][A-Za-z0-9_]*)$/)
    if (bare) names.push(bare[1])
  }
  return [...new Set(names)]
}
