import { useNavigate } from "react-router-dom"
import {
  FileChartColumnIcon,
  ExternalLinkIcon,
  FileSpreadsheetIcon,
  DatabaseIcon,
  LayoutDashboardIcon,
  SearchIcon,
  PlusIcon,
} from "lucide-react"

import { useAuth } from "@/contexts/auth-context"
import { BrandMark } from "@/components/brand"
import { FolderGlyph } from "@/components/file-type-icon"
import { accent } from "@/lib/accents"
import { NavConversations } from "@/components/nav-conversations"
import { NavMain } from "@/components/nav-main"
import { NavUser } from "@/components/nav-user"
import { Button } from "@/components/ui/button"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"

// Report Builder is opt-in per environment rather than a constant someone has
// to remember to flip back before shipping. Set VITE_SHOW_REPORT_BUILDER=true
// in frontend/.env locally, or pass it as a BUILD ARG for an image — Vite
// inlines VITE_* at build time, so a runtime container variable does nothing.
//
// The two reasons it was hidden are now addressed: the multi-block flow has
// run end to end against real Snowflake, and the scan it depends on is written
// to shared storage rather than an in-process dict, so the upload and the save
// no longer have to land on the same one of four uvicorn workers.
//
// This hides the LINK, not the feature: /admin/report-builder still resolves
// for anyone with the URL, and /excel/* is reachable by any signed-in user (it
// is not behind require_admin, unlike the delete routes). Queries there run as
// that user's own Snowflake role through validate_sql, so exposure is bounded
// by their existing grants — worth closing before this is on by default.
const SHOW_REPORT_BUILDER =
  import.meta.env.VITE_SHOW_REPORT_BUILDER === "true"

const navItems = [
  { title: "Explore", url: "/explore", icon: <SearchIcon {...accent("explore")} /> },
  { title: "Dashboards", url: "/reports/dashboards", icon: <LayoutDashboardIcon {...accent("dashboards")} /> },
  { title: "Reports", url: "/reports", icon: <FileChartColumnIcon {...accent("reports")} /> },
  // Uploaded files, kept as their own section rather than merged into
  // the chat database picker — a firm accumulates far more workbooks than
  // warehouses, and mixing them makes the picker unusable.
  { title: "Files", url: "/excel-source", icon: <FolderGlyph className="size-4" /> },
]

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const navigate = useNavigate()
  const { user } = useAuth()
  const isAdmin = Boolean(user?.is_admin)

  return (
    <Sidebar collapsible="offcanvas" {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              className="data-[slot=sidebar-menu-button]:p-1.5!"
              onClick={() => navigate("/", { state: { newChat: true } })}
            >
              <BrandMark className="h-5 w-auto" />
              <span className="text-base font-semibold">AI Reporting</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup className="gap-2">
          <Button
            onClick={() => navigate("/", { state: { newChat: true } })}
            className="justify-start gap-2"
          >
            <PlusIcon className="size-4" />
            New chat
          </Button>
        </SidebarGroup>

        <NavMain label="General" items={navItems} />

        {/* Hidden entirely for non-admins rather than shown-and-refused: the
            server enforces this, so the only thing a visible link would add is
            a dead end. Plain anchors with target=_blank, not router Links —
            the admin surface is a separate workspace, and an admin editing a
            layer usually wants the analyst app still open to test against. */}
        {isAdmin ? (
          <SidebarGroup>
            <SidebarGroupLabel>Configuration</SidebarGroupLabel>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  asChild
                  tooltip="Semantic Builder (opens a new tab)"
                >
                  <a
                    href="/admin/semantic"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <DatabaseIcon {...accent("semantic")} />
                    <span>Semantic Builder</span>
                    <ExternalLinkIcon className="ml-auto size-3.5 opacity-60" />
                  </a>
                </SidebarMenuButton>
              </SidebarMenuItem>
              {SHOW_REPORT_BUILDER ? (
                <SidebarMenuItem>
                  <SidebarMenuButton
                    asChild
                    tooltip="Report Builder (opens a new tab)"
                  >
                    <a
                      href="/admin/report-builder"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <FileSpreadsheetIcon {...accent("reports")} />
                      <span>Report Builder</span>
                      <ExternalLinkIcon className="ml-auto size-3.5 opacity-60" />
                    </a>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ) : null}
            </SidebarMenu>
          </SidebarGroup>
        ) : null}

        <NavConversations />
      </SidebarContent>

      <SidebarFooter>
        <NavUser />
      </SidebarFooter>
    </Sidebar>
  )
}
