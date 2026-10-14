import * as React from "react"
import { ArrowUpIcon, SquareIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"


/** Roughly ten lines; past that the textarea scrolls instead of eating the page. */
const MAX_COMPOSER_HEIGHT = 240

type ChatComposerProps = {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  onStop?: () => void
  isLoading?: boolean
  canSend?: boolean
  disabled?: boolean
  placeholder?: string
  className?: string
}

export function ChatComposer({
  value,
  onChange,
  onSend,
  onStop,
  isLoading = false,
  canSend = true,
  disabled = false,
  placeholder = "Ask anything about your data…",
  className,
}: ChatComposerProps) {
  const textareaRef = React.useRef<HTMLTextAreaElement>(null)

  // Grow with the content up to a ceiling, then scroll internally. A fixed
  // one-row box hides everything but the last line while you are still writing,
  // which is the wrong trade for a multi-line analytical question.
  React.useLayoutEffect(() => {
    const el = textareaRef.current
    if (!el) return
    // Collapse first: scrollHeight never shrinks on its own.
    el.style.height = "auto"
    el.style.height = `${Math.min(el.scrollHeight, MAX_COMPOSER_HEIGHT)}px`
  }, [value])

  const sendEnabled = value.trim().length > 0 && !isLoading && canSend && !disabled

  function handleKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      if (sendEnabled) {
        onSend()
      }
    }
  }

  return (
    <div
      className={cn(
        "flex flex-col rounded-2xl border border-border bg-card shadow-sm",
        className
      )}
    >
      <Textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
        className="min-h-12 resize-none overflow-y-auto border-0 bg-transparent px-4 pt-4 pb-2 shadow-none focus-visible:ring-0 dark:bg-transparent"
      />

      {/* No Q&A/Dashboard mode switch: the agent picks tools from the request
          itself, so asking the user to pre-declare intent only constrains it —
          "show me X and chart it" spans both modes anyway. */}
      <div className="flex items-center justify-end gap-2 px-3 pb-3">
        {isLoading && onStop ? (
          <Button
            type="button"
            size="icon"
            aria-label="Stop generation"
            onClick={onStop}
            className="rounded-full"
          >
            <SquareIcon className="size-4 fill-current" />
          </Button>
        ) : (
          <Button
            type="button"
            size="icon"
            aria-label="Send message"
            disabled={!sendEnabled}
            onClick={onSend}
            className="rounded-full"
          >
            <ArrowUpIcon />
          </Button>
        )}
      </div>
    </div>
  )
}
