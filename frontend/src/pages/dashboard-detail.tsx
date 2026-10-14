import * as React from "react"
import { Link, useParams } from "react-router-dom"
import {
  ArrowLeftIcon,
  ChevronDownIcon,
  DownloadIcon,
  LayoutGridIcon,
  Loader2,
  MessageSquareIcon,
  RefreshCwIcon,
  SlidersHorizontalIcon,
  XIcon,
} from "lucide-react"

import {
  ChartDisplayContext,
  dashboardCurrencyUnit,
} from "@/components/dashboard/chart-display-context"
import { DashboardChat } from "@/components/dashboard/dashboard-chat"
import { DashboardEditor } from "@/components/dashboard/dashboard-editor"
import { DashboardGrid } from "@/components/dashboard/dashboard-grid"
import { DashboardNarrative } from "@/components/dashboard/dashboard-narrative"
import { ResizableAside } from "@/components/dashboard/resizable-aside"
import {
  DashboardKpiBand,
  type DashboardPanel,
} from "@/components/dashboard/dashboard-panel"
import {
  groupBySection,
  MIN_CHART_SPAN,
  type PanelRowSpan,
  type PanelSpan,
} from "@/lib/dashboard-layout"
import {
  applyCrossFilter,
  type CrossFilter,
} from "@/lib/dashboard-cross-filter"
import { buildDashboardNarrative } from "@/lib/dashboard-narrative"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { useAuth } from "@/contexts/auth-context"
import {
  downloadDashboardExport,
  fetchDashboard,
  fetchDashboardNarrative,
  runDashboard,
  updateDashboardLayout,
  type DashboardNarrativeRequest,
  type DashboardRun,
} from "@/lib/api"
import { columnIndex } from "@/lib/dashboard-format"
import { captureDashboardForPdf } from "@/components/dashboard/pdf-capture"
import { cn } from "@/lib/utils"
import { PanelErrorBoundary } from "@/components/error-boundary"
import { parseUtcTimestamp } from "@/lib/time"

export function DashboardDetail() {
  const { reportId = "" } = useParams()
  const [data, setData] = React.useState<DashboardRun | null>(null)
  const [isLoading, setIsLoading] = React.useState(true)
  const [isRefreshing, setIsRefreshing] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [paramValues, setParamValues] = React.useState<Record<string, string>>(
    {}
  )
  // Closed on first visit — the feature costs one button until you want it.
  // Remembered after that, the same way the dashboards list remembers its view:
  // whether you work with the assistant open is a habit, not a per-visit choice.
  const [isChatOpen, setIsChatOpen] = React.useState(
    () => localStorage.getItem("dashboard-chat-open") === "true"
  )
  // NOT remembered, unlike the chat: editing is a deliberate errand, and
  // arriving with the form already open would read as an invitation to change
  // something rather than a tool you reached for.
  const [isEditOpen, setEditOpen] = React.useState(false)
  const { user } = useAuth()
  const isAdmin = Boolean(user?.is_admin)

  const [activeTab, setActiveTab] = React.useState<string>("")
  // The arranged-but-unsaved panel list: a whole array rather than per-index
  // overrides, because reordering changes positions and every downstream step
  // (grouping into tabs, laying out a section, building the save payload)
  // already works off a panel array. null means "no changes yet", which is also
  // what tells the Save button there is nothing to save.
  const [draftPanels, setDraftPanels] = React.useState<DashboardPanel[] | null>(
    null
  )
  const [isArranging, setArranging] = React.useState(false)
  const [isSavingLayout, setSavingLayout] = React.useState(false)
  const [isExporting, setExporting] = React.useState(false)
  const [crossFilter, setCrossFilter] = React.useState<CrossFilter>(null)
  const [aiNarrative, setAiNarrative] = React.useState<string | null>(null)
  const [narrativeLoading, setNarrativeLoading] = React.useState(false)

  const setChatOpen = React.useCallback((open: boolean) => {
    setIsChatOpen(open)
    localStorage.setItem("dashboard-chat-open", String(open))
  }, [])

  const load = React.useCallback(
    async (values?: Record<string, string>) => {
      setError(null)
      try {
        const result = values
          ? await runDashboard(reportId, values)
          : await fetchDashboard(reportId)
        setData(result)
        setCrossFilter(null)
        setAiNarrative(null)
        // Seed the controls from what actually ran, so the inputs always
        // reflect the data on screen rather than a stale guess.
        setParamValues(
          Object.fromEntries(
            Object.entries(result.parameter_values ?? {}).map(([k, v]) => [
              k,
              v === null || v === undefined ? "" : String(v),
            ])
          )
        )
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load")
      }
    },
    [reportId]
  )

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      await load()
      if (!cancelled) setIsLoading(false)
    })()
    return () => {
      cancelled = true
    }
  }, [load])

  async function refresh() {
    setIsRefreshing(true)
    // Blank fields are omitted so the server falls back to saved defaults.
    const values = Object.fromEntries(
      Object.entries(paramValues).filter(([, v]) => v !== "")
    )
    await load(Object.keys(values).length ? values : undefined)
    setIsRefreshing(false)
  }

  async function exportFile(format: "excel" | "pdf") {
    setExporting(true)
    setError(null)
    try {
      // The PDF carries the summary shown on the page, a picture of every
      // chart as the dashboard draws it, and each table as it is written on
      // screen. Excel gets none of them: it is a data file, and its sheets
      // are the panels' rows.
      const capture =
        format === "pdf" ? await captureDashboardForPdf(panels) : undefined
      await downloadDashboardExport(
        reportId,
        format,
        format === "pdf" ? narrative : null,
        capture?.images,
        capture?.tables
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed")
    } finally {
      setExporting(false)
    }
  }

  const panels = React.useMemo(
    () => (data?.panels ?? []) as DashboardPanel[],
    [data]
  )
  // One currency unit across the dashboard's currency charts, so millions on
  // one panel and billions on the next do not have to be converted by eye.
  const chartDisplay = React.useMemo(
    () => ({
      currencyUnit: dashboardCurrencyUnit((data?.panels ?? []) as DashboardPanel[]),
    }),
    [data]
  )

  // AI narrative once per successful load (rule-based fallback stays available).
  React.useEffect(() => {
    if (!data || !reportId) return
    let cancelled = false
    const runPanels = (data.panels ?? []) as DashboardPanel[]
    const kpis: Array<{ metric: unknown; value: unknown; format?: unknown }> = []
    const snaps: DashboardNarrativeRequest["panels"] = []
    for (const panel of runPanels) {
      const type = (panel.chart_type ?? "").toLowerCase()
      const display = (panel.display ?? "").toLowerCase()
      if (type === "kpi" || display === "kpi") {
        const mi = columnIndex(panel.columns, "METRIC")
        const vi = columnIndex(panel.columns, "VALUE")
        const fi = columnIndex(panel.columns, "FORMAT")
        const metricIdx = mi >= 0 ? mi : 0
        const valueIdx = vi >= 0 ? vi : 1
        for (const row of panel.rows) {
          kpis.push({
            metric: row[metricIdx],
            value: row[valueIdx],
            format: fi >= 0 ? row[fi] : undefined,
          })
        }
      } else {
        snaps.push({
          title: panel.title,
          chart_type: panel.chart_type ?? panel.display,
          // All of the panel's rows and how it reads them, not a sample: the
          // first five rows of a series ordered oldest-first are its OLDEST
          // five, and the summary described early 2025 as the current trend.
          // The server reduces these to figures before the model sees them.
          x: panel.x,
          y: panel.y,
          color: panel.color,
          columns: panel.columns,
          rows: panel.rows,
          column_formats: panel.column_formats ?? null,
          default_currency: panel.default_currency ?? null,
          row_count: panel.row_count,
          // The narrative is written from this snapshot, and a capped panel
          // reads as a complete one unless the cap travels with it.
          truncated: Boolean(panel.truncated),
        })
      }
    }
    setNarrativeLoading(true)
    void fetchDashboardNarrative(reportId, {
      title: data.title ?? "Dashboard",
      default_currency:
        runPanels.find((p) => p.default_currency)?.default_currency ?? null,
      kpis,
      panels: snaps,
    })
      .then((text) => {
        if (!cancelled) setAiNarrative(text)
      })
      .catch(() => {
        if (!cancelled) setAiNarrative(null)
      })
      .finally(() => {
        if (!cancelled) setNarrativeLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [data, reportId])

  const working = draftPanels ?? panels
  const filteredWorking = React.useMemo(
    () =>
      crossFilter
        ? working.map((p) => applyCrossFilter(p, crossFilter))
        : working,
    [working, crossFilter]
  )
  const sections = React.useMemo(
    () => groupBySection(filteredWorking),
    [filteredWorking]
  )

  // Sections come from the data, so the active tab has to be reconciled once it
  // arrives — and again if an edit renames or removes the tab that was open.
  React.useEffect(() => {
    if (sections.length === 0) return
    const names = sections.map((s) => s.name ?? "")
    if (!names.includes(activeTab)) setActiveTab(names[0])
  }, [sections, activeTab])

  const onResize = React.useCallback(
    (index: number, span: PanelSpan, rowSpan: PanelRowSpan) => {
      const nextSpan = (span < MIN_CHART_SPAN ? MIN_CHART_SPAN : span) as PanelSpan
      setDraftPanels((prev) =>
        (prev ?? panels).map((panel, i) =>
          i === index ? { ...panel, span: nextSpan, row_span: rowSpan } : panel
        )
      )
    },
    [panels]
  )

  /** Move a panel to sit where another one currently is. */
  const onReorder = React.useCallback(
    (from: number, to: number) => {
      setDraftPanels((prev) => {
        const list = [...(prev ?? panels)]
        if (from === to || from < 0 || to < 0 || from >= list.length) return prev
        const [moved] = list.splice(from, 1)
        // Insert at `to`, not `to - 1`: the panel should take the slot the
        // target occupied. Compensating for the removal lands it one short, and
        // dragging onto the adjacent panel then does nothing.
        list.splice(to, 0, moved)
        return list
      })
    },
    [panels]
  )

  const saveLayout = React.useCallback(async () => {
    setSavingLayout(true)
    setError(null)
    try {
      // Body panels in display order, then KPIs with null spans so they stay
      // in the top band and are never re-locked into a quarter-width grid cell.
      const layout = sections.flatMap((section) => [
        ...section.panels.map((item) => ({
          title: item.panel.title,
          section: section.name,
          span: item.span,
          row_span: item.rowSpan,
        })),
        ...section.kpis.map((panel) => ({
          title: panel.title,
          section: section.name,
          span: null,
          row_span: null,
        })),
      ])
      await updateDashboardLayout(reportId, layout)
      setDraftPanels(null)
      setArranging(false)
      await load(paramValues)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the layout")
    } finally {
      setSavingLayout(false)
    }
  }, [sections, reportId, load, paramValues])

  const onCategoryClick = React.useCallback((value: string) => {
    setCrossFilter((prev) =>
      prev?.value === value ? null : { value }
    )
  }, [])

  if (isLoading) {
    return (
      <div className="mx-auto w-full max-w-6xl space-y-4 p-6">
        <Skeleton className="h-8 w-64" />
        <div className="@container/panels grid gap-4">
          <Skeleton className="h-56" />
          <Skeleton className="h-56" />
        </div>
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="mx-auto w-full max-w-6xl p-6">
        <Link
          to="/reports/dashboards"
          className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeftIcon className="size-4" /> All dashboards
        </Link>
        <p className="text-sm text-destructive">{error}</p>
      </div>
    )
  }

  const parameters = data?.parameters ?? []
  const active =
    sections.find((s) => (s.name ?? "") === activeTab) ?? sections[0] ?? null
  const hasTabs = sections.length > 1
  const hasDraft = draftPanels !== null
  const fallbackNarrative =
    active && active.kpis.length > 0
      ? buildDashboardNarrative(active.kpis)
      : null
  const narrative = aiNarrative ?? fallbackNarrative

  return (
    // AppShell puts overflow-hidden on the outlet container (chat manages its
    // own ScrollArea), so the scrolling happens on the dashboard column below.
    <div className="flex h-full min-h-0 overflow-hidden">
      <div
        className={cn(
          "scrollbar-slim min-w-0 flex-1 overflow-y-auto",
          // Below md there is not enough width for both. The chat takes the
          // screen rather than squeezing the dashboard into an unreadable strip.
          isChatOpen && "hidden md:block"
        )}
      >
        <div className="mx-auto w-full max-w-6xl space-y-5 p-6">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <Link
                to="/reports/dashboards"
                className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
              >
                <ArrowLeftIcon className="size-4" /> All dashboards
              </Link>
              <h1 className="mt-2 text-xl font-medium">
                {data?.title ?? "Dashboard"}
              </h1>
              <p className="text-xs text-muted-foreground">
                {data?.run_count ?? 0} runs
                {data?.last_run_at
                  ? ` · last run ${parseUtcTimestamp(data.last_run_at).toLocaleString()}`
                  : ""}
              </p>
            </div>

            <div className="flex shrink-0 items-center gap-2">
              {/* Arranging takes over the header. Refresh, Ask and Edit all
                  leave: they act on the data or open a side panel, and neither
                  is what you are doing while moving panels around. What is left
                  is the only decision that matters — keep it or discard it. */}
              {isArranging ? (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setDraftPanels(null)
                      setArranging(false)
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    size="sm"
                    disabled={!hasDraft || isSavingLayout}
                    onClick={saveLayout}
                    className="gap-2"
                  >
                    {isSavingLayout ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : null}
                    Save
                  </Button>
                </>
              ) : null}

              {/* Admins only, and hidden rather than disabled: editing changes
                  what everyone sees, so it matches delete. The server refuses
                  the PUT regardless of what is rendered. */}
              {isAdmin && !isChatOpen && !isEditOpen && !isArranging ? (
                <>
                  {/* Arranging happens in the grid itself rather than a side
                      panel: you are judging the result while you drag, so the
                      dashboard needs the full width. */}
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-2"
                    onClick={() => {
                      setCrossFilter(null)
                      setArranging(true)
                    }}
                  >
                    <LayoutGridIcon className="size-4" />
                    Arrange
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-2"
                    onClick={() => setEditOpen(true)}
                  >
                    <SlidersHorizontalIcon className="size-4" />
                    Edit
                  </Button>
                </>
              ) : null}
              {/* The only affordance the feature adds when closed: one button. */}
              {!isChatOpen && !isArranging ? (
                <>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="outline"
                        size="sm"
                        className="gap-2"
                        disabled={isExporting || isRefreshing}
                      >
                        {isExporting ? (
                          <Loader2 className="size-4 animate-spin" />
                        ) : (
                          <DownloadIcon className="size-4" />
                        )}
                        Download
                        <ChevronDownIcon className="size-3.5 opacity-70" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem
                        onClick={() => void exportFile("excel")}
                      >
                        Excel (.xlsx)
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        onClick={() => void exportFile("pdf")}
                      >
                        PDF
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-2"
                    onClick={() => {
                      setEditOpen(false)
                      setChatOpen(true)
                    }}
                  >
                    <MessageSquareIcon className="size-4" />
                    Ask
                  </Button>
                </>
              ) : null}
            </div>
          </div>

          {/* A filter bar, not loose inputs. Given a surface of its own the
              controls read as the dashboard's parameters; scattered above the
              panels they read as page furniture. Hidden while arranging: it
              re-runs queries, which is not what a layout edit is about. */}
          <div
            hidden={isArranging}
            className={cn(
              "flex flex-wrap items-end gap-3",
              parameters.length > 0 && "rounded-lg border bg-muted/30 p-3"
            )}
          >
            {parameters.map((p) => (
              <div key={p.name} className="flex flex-col gap-1.5">
                <Label
                  htmlFor={p.name}
                  className="text-xs text-muted-foreground"
                >
                  {p.name}
                </Label>
                {p.type === "enum" && Array.isArray(p.options) ? (
                  <Select
                    value={paramValues[p.name] ?? ""}
                    onValueChange={(v) =>
                      setParamValues((prev) => ({ ...prev, [p.name]: v }))
                    }
                  >
                    <SelectTrigger id={p.name} className="w-[180px]">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {p.options.map((o) => (
                        <SelectItem key={o} value={o}>
                          {o}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <Input
                    id={p.name}
                    type={
                      p.type === "date"
                        ? "date"
                        : p.type === "number"
                          ? "number"
                          : "text"
                    }
                    value={paramValues[p.name] ?? ""}
                    onChange={(e) =>
                      setParamValues((prev) => ({
                        ...prev,
                        [p.name]: e.target.value,
                      }))
                    }
                    className="w-[180px]"
                  />
                )}
              </div>
            ))}

            {/* Pushed right: it acts on the whole dashboard, not on the parameter
            field immediately beside it. */}
            <Button
              onClick={refresh}
              disabled={isRefreshing}
              className="ml-auto gap-2"
            >
              {isRefreshing ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <RefreshCwIcon className="size-4" />
              )}
              Refresh
            </Button>
          </div>

          {error ? <p className="text-sm text-destructive">{error}</p> : null}

          {crossFilter && !isArranging ? (
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border/60 bg-muted/40 px-3 py-2 text-sm">
              <span className="text-muted-foreground">Filtered to</span>
              <span className="font-medium">{crossFilter.value}</span>
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto h-7 gap-1 px-2 text-xs"
                onClick={() => setCrossFilter(null)}
              >
                <XIcon className="size-3.5" />
                Clear
              </Button>
            </div>
          ) : null}

          {/* One tab bar only when the planner actually grouped the panels.
              A single tab is not a tab, and rendering one would add a control
              that does nothing. */}
          {hasTabs ? (
            <Tabs value={activeTab} onValueChange={setActiveTab}>
              <TabsList>
                {sections.map((section) => (
                  <TabsTrigger
                    key={section.name ?? ""}
                    value={section.name ?? ""}
                  >
                    {section.name ?? "Overview"}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
          ) : null}

          {/* KPI panels sit in a band above the chart grid — Arrange only
              moves body charts/tables, so headline numbers stay scannable. */}
          {active && active.kpis.length > 0 ? (
            <div className="space-y-3">
              <PanelErrorBoundary title="Headline figures" resetKey={active.kpis}>
                <DashboardKpiBand panels={active.kpis} />
              </PanelErrorBoundary>
              {!isArranging ? (
                narrativeLoading && !narrative ? (
                  <p className="text-[13px] text-muted-foreground/70">
                    Writing summary…
                  </p>
                ) : narrative ? (
                  <DashboardNarrative text={narrative} />
                ) : null
              ) : null}
            </div>
          ) : null}
          <ChartDisplayContext.Provider value={chartDisplay}>
            <DashboardGrid
              panels={active?.panels ?? []}
              editable={isArranging}
              onResize={onResize}
              onReorder={onReorder}
              onCategoryClick={onCategoryClick}
              selectedValue={crossFilter?.value ?? null}
            />
          </ChartDisplayContext.Provider>
        </div>
      </div>

      {/* One side panel at a time. Ask and Edit want the same space, and two
          open at once would leave the dashboard itself a strip. */}
      {isChatOpen ? (
        // Its left edge drags, so a long answer or a wide table can be given
        // more room; the width is remembered in this browser.
        <ResizableAside
          storageKey="dashboard-chat-width"
          defaultWidthClassName="md:w-[380px] lg:w-[420px]"
          className="border-l md:border-l-0"
        >
          <DashboardChat
            reportId={reportId}
            title={data?.title ?? null}
            onClose={() => setChatOpen(false)}
          />
        </ResizableAside>
      ) : isEditOpen ? (
        // Drags wider like the Ask panel, with its own remembered width: a form
        // with SQL in it and a chat beside it want different room.
        <ResizableAside
          storageKey="dashboard-edit-width"
          defaultWidthClassName="md:w-[420px] lg:w-[480px]"
        >
          <DashboardEditor
            reportId={reportId}
            onClose={() => setEditOpen(false)}
            // Re-runs with the parameters currently on screen, so the panel
            // you just edited redraws with real data without a page reload.
            onSaved={() => load(paramValues)}
          />
        </ResizableAside>
      ) : null}
    </div>
  )
}

export default DashboardDetail
