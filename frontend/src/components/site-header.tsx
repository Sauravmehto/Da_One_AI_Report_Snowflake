import { useLocation } from "react-router-dom"

import { ChatHeaderDatabase } from "@/components/chat/chat-header-database"
import { Separator } from "@/components/ui/separator"
import { SidebarTrigger } from "@/components/ui/sidebar"

const TITLES: Record<string, string> = {
  "/": "New",
  "/explore": "Explore",
  "/charts": "Charts",
  "/reports": "Reports",
}

export function SiteHeader({ title }: { title?: string }) {
  const location = useLocation()
  const resolvedTitle = title ?? TITLES[location.pathname] ?? "AI Reporting"

  return (
    <header className="flex h-(--header-height) shrink-0 items-center gap-2 border-b transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-(--header-height)">
      <div className="flex w-full min-w-0 items-center gap-1 px-4 lg:gap-2 lg:px-6">
        <SidebarTrigger className="-ml-1" />
        <Separator
          orientation="vertical"
          className="mx-2 data-[orientation=vertical]:h-4"
        />
        <h1 className="shrink-0 text-base font-medium">{resolvedTitle}</h1>
        <ChatHeaderDatabase />
      </div>
    </header>
  )
}
