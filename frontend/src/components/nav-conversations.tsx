import * as React from "react"
import {
  MessageSquareIcon,
  MoreHorizontalIcon,
  PinIcon,
  PinOffIcon,
  Trash2Icon,
  PencilIcon,
} from "lucide-react"
import { useNavigate } from "react-router-dom"

import { useConversations } from "@/contexts/conversations-context"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarGroup,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import { cn } from "@/lib/utils"

function RenameInput({
  initialValue,
  onCommit,
  onCancel,
}: {
  initialValue: string
  onCommit: (value: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = React.useState(initialValue)
  const ref = React.useRef<HTMLInputElement>(null)

  React.useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
  }, [])

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault()
      onCommit(value.trim() || initialValue)
    } else if (e.key === "Escape") {
      onCancel()
    }
  }

  return (
    <input
      ref={ref}
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={handleKeyDown}
      onBlur={() => onCommit(value.trim() || initialValue)}
      className="w-full truncate rounded border border-border bg-background px-1 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
    />
  )
}

export function NavConversations() {
  const navigate = useNavigate()
  const { conversations, activeId, remove, rename, togglePin } =
    useConversations()
  const [renamingId, setRenamingId] = React.useState<string | null>(null)

  return (
    <SidebarGroup>
      <SidebarGroupLabel>Chats</SidebarGroupLabel>

      {/* The heading stays put when there is nothing to list: the section is
          one of three fixed parts of the sidebar, and a group that appears
          only once you happen to have history reads as a layout glitch. */}
      {conversations.length === 0 ? (
        <p className="px-2 py-1.5 text-xs text-muted-foreground">
          No chats yet. Start one with "New chat".
        </p>
      ) : null}

      <SidebarMenu>
        {conversations.map((c) => (
          <SidebarMenuItem key={c.conversation_id} className="group/chat">
            <SidebarMenuButton
              isActive={c.conversation_id === activeId}
              onClick={() => {
                if (renamingId === c.conversation_id) return
                navigate("/", {
                  state: { resumeConversationId: c.conversation_id },
                })
              }}
              className="pr-8"
            >
              {c.pinned ? (
                <PinIcon className="size-3.5 shrink-0 text-muted-foreground" />
              ) : (
                <MessageSquareIcon className="size-4 shrink-0" />
              )}
              {renamingId === c.conversation_id ? (
                <RenameInput
                  initialValue={c.title ?? ""}
                  onCommit={(title) => {
                    void rename(c.conversation_id, title)
                    setRenamingId(null)
                  }}
                  onCancel={() => setRenamingId(null)}
                />
              ) : (
                <span className="truncate">{c.title || "Untitled chat"}</span>
              )}
            </SidebarMenuButton>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  aria-label="Chat options"
                  onClick={(e) => e.stopPropagation()}
                  className={cn(
                    "absolute top-1/2 right-1 -translate-y-1/2 rounded p-1",
                    "text-muted-foreground opacity-0 transition-opacity",
                    "hover:bg-sidebar-accent hover:text-foreground",
                    "group-hover/chat:opacity-100 focus-visible:opacity-100"
                  )}
                >
                  <MoreHorizontalIcon className="size-3.5" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent side="right" align="start" className="w-40">
                <DropdownMenuItem
                  onClick={() => setRenamingId(c.conversation_id)}
                >
                  <PencilIcon className="mr-2 size-3.5" />
                  Rename
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => void togglePin(c.conversation_id)}
                >
                  {c.pinned ? (
                    <>
                      <PinOffIcon className="mr-2 size-3.5" />
                      Unpin
                    </>
                  ) : (
                    <>
                      <PinIcon className="mr-2 size-3.5" />
                      Pin
                    </>
                  )}
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  className="text-destructive focus:text-destructive"
                  onClick={() => void remove(c.conversation_id)}
                >
                  <Trash2Icon className="mr-2 size-3.5" />
                  Delete
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        ))}
      </SidebarMenu>
    </SidebarGroup>
  )
}
