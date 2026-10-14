import * as React from "react"
import ReactMarkdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"

import { QueryResultsTable } from "@/components/report/widgets/table-widget"
import {
  DashboardPanelCard,
  type DashboardPanel,
} from "@/components/dashboard/dashboard-panel"
import { CHAT_COLUMN } from "@/components/chat/chat-layout"
import {
  ToolStepRow,
  type ToolActivity,
} from "@/components/chat/tool-activity"
import { Skeleton } from "@/components/ui/skeleton"
import type { ChatData } from "@/lib/api"
import { cn } from "@/lib/utils"
import { PanelErrorBoundary } from "@/components/error-boundary"

/** Chronological pieces of one assistant turn — text, tools, charts in order. */
export type AssistantBlock =
  | { type: "text"; id: string; content: string }
  | { type: "tool"; id: string; activity: ToolActivity }
  | { type: "chart"; id: string; panel: DashboardPanel }

export type ChatMessage = {
  id: string
  role: "user" | "assistant"
  content: string
  sql?: string | null
  data?: ChatData | null
  /**
   * Ordered stream of the turn. When present, the UI renders this instead of
   * dumping tools above all text.
   */
  blocks?: AssistantBlock[]
  /** Tool calls — kept in sync with blocks for callers that still read it. */
  tools?: ToolActivity[]
  /** Charts — kept in sync with blocks for callers that still read it. */
  charts?: DashboardPanel[]
  /** The model's latest reasoning summary, while it is still thinking.
   *  Cleared once real text arrives — it is a progress cue, not content. */
  thinking?: string | null
  /** True while this message is still streaming in. */
  streaming?: boolean
  /** True when the user stopped generation mid-turn. */
  stopped?: boolean
  isError?: boolean
}

type ChatMessagesProps = {
  messages: ChatMessage[]
  isLoading?: boolean
  className?: string
}

export function blocksContent(blocks: AssistantBlock[]): string {
  return blocks
    .filter((b): b is Extract<AssistantBlock, { type: "text" }> => b.type === "text")
    .map((b) => b.content)
    .filter(Boolean)
    .join("\n\n")
}

export function blocksTools(blocks: AssistantBlock[]): ToolActivity[] {
  return blocks
    .filter((b): b is Extract<AssistantBlock, { type: "tool" }> => b.type === "tool")
    .map((b) => b.activity)
}

export function blocksCharts(blocks: AssistantBlock[]): DashboardPanel[] {
  return blocks
    .filter((b): b is Extract<AssistantBlock, { type: "chart" }> => b.type === "chart")
    .map((b) => b.panel)
}

/** Mirror legacy fields from chronological blocks so older readers stay correct. */
export function withSyncedFields(
  message: ChatMessage,
  blocks: AssistantBlock[]
): ChatMessage {
  return {
    ...message,
    blocks,
    content: blocksContent(blocks),
    tools: blocksTools(blocks),
    charts: blocksCharts(blocks),
  }
}

/**
 * Append a text chunk. Continues the last text block when possible; starts a
 * new one after a tool/chart so tools sit between paragraphs chronologically.
 */
export function appendTextBlock(
  blocks: AssistantBlock[],
  chunk: string,
  newId: () => string,
  forceNew = false
): AssistantBlock[] {
  const next = [...blocks]
  const last = next[next.length - 1]
  if (!forceNew && last?.type === "text") {
    next[next.length - 1] = { ...last, content: last.content + chunk }
    return next
  }
  if (!chunk) return next
  next.push({ type: "text", id: newId(), content: chunk })
  return next
}

export function pushToolBlock(
  blocks: AssistantBlock[],
  activity: ToolActivity
): AssistantBlock[] {
  return [...blocks, { type: "tool", id: activity.id, activity }]
}

export function updateToolBlock(
  blocks: AssistantBlock[],
  toolId: string,
  patch: Partial<ToolActivity>
): AssistantBlock[] {
  return blocks.map((b) =>
    b.type === "tool" && b.activity.id === toolId
      ? { ...b, activity: { ...b.activity, ...patch } }
      : b
  )
}

export function pushChartBlock(
  blocks: AssistantBlock[],
  panel: DashboardPanel,
  newId: () => string
): AssistantBlock[] {
  return [...blocks, { type: "chart", id: newId(), panel }]
}

const markdownComponents: Components = {
  h1: ({ children }) => (
    <h1 className="mt-6 mb-4 text-2xl font-semibold text-foreground first:mt-0">
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-5 mb-3 text-xl font-semibold text-foreground first:mt-0">
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-4 mb-2 text-lg font-semibold text-foreground first:mt-0">
      {children}
    </h3>
  ),
  p: ({ children }) => (
    <p className="mb-3 leading-relaxed break-words text-foreground last:mb-0">
      {children}
    </p>
  ),
  ul: ({ children }) => (
    <ul className="mb-3 list-disc space-y-1 pl-5 text-foreground">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-3 list-decimal space-y-1 pl-5 text-foreground">
      {children}
    </ol>
  ),
  li: ({ children }) => (
    <li className="break-words text-foreground">{children}</li>
  ),
  strong: ({ children }) => (
    <strong className="font-semibold text-foreground">{children}</strong>
  ),
  em: ({ children }) => <em className="text-foreground italic">{children}</em>,
  blockquote: ({ children }) => (
    <blockquote className="mb-3 border-l-2 border-border pl-4 text-muted-foreground">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-4 border-border" />,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="font-medium text-foreground underline underline-offset-4"
    >
      {children}
    </a>
  ),
  pre: ({ children }) => (
    <pre className="mb-3 max-w-full overflow-x-auto rounded-lg border bg-muted p-3 text-sm">
      {children}
    </pre>
  ),
  code: ({ className, children }) => {
    const isBlock = className?.includes("language-")
    if (isBlock) {
      return (
        <code className={cn("font-mono text-foreground", className)}>
          {children}
        </code>
      )
    }
    return (
      <code className="rounded bg-muted px-1 py-0.5 font-mono text-sm break-words text-foreground">
        {children}
      </code>
    )
  },
  table: ({ children }) => (
    <div className="mb-3 max-w-full overflow-x-auto rounded-lg border">
      <table className="w-full min-w-max text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="border-b bg-muted/50">{children}</thead>
  ),
  tbody: ({ children }) => <tbody>{children}</tbody>,
  tr: ({ children }) => <tr className="border-b">{children}</tr>,
  th: ({ children }) => (
    <th className="h-10 px-2 text-left align-middle font-medium whitespace-nowrap text-foreground">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="p-2 align-middle whitespace-nowrap text-foreground">
      {children}
    </td>
  ),
}

function AssistantContent({
  content,
  sql,
  data,
}: {
  content: string
  sql?: string | null
  data?: ChatData | null
}) {
  return (
    <div className="w-full max-w-full min-w-0 text-foreground">
      <div className="max-w-full break-words">
        <ReactMarkdown
          // singleTilde MUST stay false. GFM's default treats a lone `~` as a
          // strikethrough delimiter, and the assistant writes `~` for
          // "approximately" all the time — "~$57.3M … (~$308K/2 quarters)".
          // Two of those in one paragraph pair up and strike out everything
          // between them, so a correct answer renders as though the model had
          // retracted it. Nothing warns you: it is valid GFM, it only ever hits
          // paragraphs with an even number of tildes, and the struck text still
          // reads as English. Requiring `~~` costs nothing — real strikethrough
          // has no place in these answers anyway.
          remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
          components={markdownComponents}
        >
          {content}
        </ReactMarkdown>
      </div>
      {sql ? (
        <details className="mt-4 w-full min-w-0 text-xs text-muted-foreground">
          <summary className="cursor-pointer font-medium text-foreground">
            View SQL
          </summary>
          <pre className="mt-2 max-w-full overflow-x-auto rounded-lg border bg-muted/50 p-3 text-xs break-words whitespace-pre-wrap">
            {sql}
          </pre>
        </details>
      ) : null}
      {data ? (
        <QueryResultsTable
          data={data}
          className="mt-4 w-full min-w-0 space-y-2"
        />
      ) : null}
    </div>
  )
}

/** The tail of a reasoning summary, short enough to sit on one line. */
function lastLine(thinking: string | null | undefined): string | null {
  if (!thinking) return null
  const lines = thinking.split("\n").filter((l) => l.trim())
  const last = lines[lines.length - 1]?.trim()
  if (!last) return null
  return last.length > 110 ? `${last.slice(0, 110)}…` : last
}

function ThinkingDots({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <span
        className="inline-block size-1.5 rounded-full bg-muted-foreground/60 animate-bounce"
        style={{ animationDelay: "0ms" }}
      />
      <span
        className="inline-block size-1.5 rounded-full bg-muted-foreground/60 animate-bounce"
        style={{ animationDelay: "150ms" }}
      />
      <span
        className="inline-block size-1.5 rounded-full bg-muted-foreground/60 animate-bounce"
        style={{ animationDelay: "300ms" }}
      />
      <span className="ml-1">{label}</span>
    </div>
  )
}

/**
 * Prefer chronological blocks; fall back to legacy tools→charts→content for
 * any message that was built before blocks existed.
 */
function resolveBlocks(message: ChatMessage): AssistantBlock[] {
  if (message.blocks && message.blocks.length > 0) return message.blocks

  const fallback: AssistantBlock[] = []
  for (const tool of message.tools ?? []) {
    fallback.push({ type: "tool", id: tool.id, activity: tool })
  }
  for (const panel of message.charts ?? []) {
    fallback.push({ type: "chart", id: `chart-${fallback.length}`, panel })
  }
  if (message.content) {
    fallback.push({ type: "text", id: `text-${message.id}`, content: message.content })
  }
  return fallback
}

function StreamingHint({ message }: { message: ChatMessage }) {
  if (!message.streaming) return null

  const blocks = resolveBlocks(message)
  const tools = blocksTools(blocks)
  const hasText = blocks.some((b) => b.type === "text" && b.content)
  const running = tools.some((t) => t.status === "running")

  // A running tool row is already the progress signal.
  if (running) return null
  if (!hasText && tools.length === 0) {
    return <ThinkingDots label="Thinking…" />
  }
  // After tools finish (or between tool and next text) keep a soft cue at the
  // bottom so the turn does not look frozen. When the model is reasoning, say
  // what about — a turn that thinks for a minute with only bouncing dots is
  // indistinguishable from one that has died.
  const reasoning = lastLine(message.thinking)
  if (reasoning) return <ThinkingDots label={reasoning} />
  return <ThinkingDots label="Generating response…" />
}

export function ChatMessages({
  messages,
  isLoading = false,
  className,
}: ChatMessagesProps) {
  return (
    <div
      className={cn(CHAT_COLUMN, "flex flex-col gap-6 py-6", className)}
    >
      {messages.map((message) =>
        message.role === "user" ? (
          <div key={message.id} className="flex min-w-0 justify-end">
            <div className="max-w-[80%] rounded-2xl bg-muted px-4 py-2 break-words text-foreground">
              {message.content}
            </div>
          </div>
        ) : (
          <div
            key={message.id}
            className="flex w-full min-w-0 flex-col items-start gap-2"
          >
            {resolveBlocks(message).map((block, index, all) => {
              if (block.type === "text") {
                if (!block.content) return null
                // Attach sql/data only to the last text block so tables don't
                // repeat under every paragraph of a multi-block turn.
                let lastTextIndex = -1
                for (let i = all.length - 1; i >= 0; i--) {
                  if (all[i].type === "text") {
                    lastTextIndex = i
                    break
                  }
                }
                const isLastText = lastTextIndex === index
                return (
                  <div
                    key={block.id}
                    className={cn(
                      "w-full min-w-0",
                      message.isError && "text-destructive"
                    )}
                  >
                    <AssistantContent
                      content={block.content}
                      sql={isLastText ? message.sql : null}
                      data={isLastText ? message.data : null}
                    />
                  </div>
                )
              }
              if (block.type === "tool") {
                return (
                  <div key={block.id} className="w-full min-w-0">
                    <ToolStepRow activity={block.activity} />
                  </div>
                )
              }
              return (
                <div key={block.id} className="mb-1 w-full min-w-0">
                  <PanelErrorBoundary title={block.panel.title} resetKey={block.panel}>
                    <DashboardPanelCard panel={block.panel} />
                  </PanelErrorBoundary>
                </div>
              )
            })}

            <StreamingHint message={message} />

            {message.stopped ? (
              <p className="text-xs italic text-muted-foreground">
                {message.content || (message.tools?.length ?? 0) > 0
                  ? "You stopped this response."
                  : "Generation stopped before a response was produced."}
              </p>
            ) : null}
          </div>
        )
      )}

      {/* Streaming messages render their own progress, so the skeleton is only
          for the gap before the first event arrives. */}
      {isLoading && !messages.some((m) => m.streaming) && (
        <div className="space-y-2">
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-5/6" />
        </div>
      )}
    </div>
  )
}

export function useScrollToBottom(
  dependency: unknown,
  scrollRef: React.RefObject<HTMLDivElement | null>
) {
  const pinnedRef = React.useRef(true)

  React.useEffect(() => {
    const viewport = scrollRef.current?.querySelector(
      "[data-slot=scroll-area-viewport]"
    ) as HTMLElement | null
    if (!viewport) return

    const onScroll = () => {
      const distance =
        viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight
      pinnedRef.current = distance < 96
    }
    viewport.addEventListener("scroll", onScroll, { passive: true })
    return () => viewport.removeEventListener("scroll", onScroll)
  }, [scrollRef])

  React.useEffect(() => {
    if (!pinnedRef.current) return
    const viewport = scrollRef.current?.querySelector(
      "[data-slot=scroll-area-viewport]"
    )
    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight
    }
  }, [dependency, scrollRef])
}
