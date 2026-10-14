import type { CSSProperties } from "react"
import { Outlet } from "react-router-dom"

import { AppSidebar } from "@/components/app-sidebar"
import { PageErrorBoundary } from "@/components/error-boundary"
import {
  SidebarResizer,
  useSidebarWidth,
} from "@/components/sidebar-resizer"
import { SiteHeader } from "@/components/site-header"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import { ChatProvider } from "@/contexts/chat-context"

export function AppShell() {
  const [sidebarWidth, setSidebarWidth] = useSidebarWidth()

  return (
    <SidebarProvider
      className="h-svh overflow-hidden"
      style={
        {
          "--sidebar-width": `${sidebarWidth}px`,
          "--header-height": "calc(var(--spacing) * 12)",
        } as CSSProperties
      }
    >
      <AppSidebar variant="inset" />
      <SidebarResizer width={sidebarWidth} onWidthChange={setSidebarWidth} />
      <SidebarInset className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <ChatProvider>
          <SiteHeader />
          <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
            <PageErrorBoundary>
              <Outlet />
            </PageErrorBoundary>
          </div>
        </ChatProvider>
      </SidebarInset>
    </SidebarProvider>
  )
}
