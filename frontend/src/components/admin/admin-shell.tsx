import { Link, Outlet } from "react-router-dom"
import {
  ExternalLinkIcon,
  DatabaseIcon,
  FileSpreadsheetIcon,
  ShieldAlertIcon,
} from "lucide-react"
import { accent } from "@/lib/accents"

import { useAuth } from "@/contexts/auth-context"
import { PageErrorBoundary } from "@/components/error-boundary"

/**
 * Layout for the admin surface.
 *
 * Deliberately not AppShell: this opens in its own tab and is a different job
 * from asking questions of data — no chat sidebar, no conversation history, no
 * database lock. The visual break is the point. Someone editing what a column
 * *means* should be able to tell at a glance that they are not in the
 * analyst-facing app.
 */
export function AdminShell() {
  const { user, isLoading } = useAuth()

  return (
    <div className="flex h-svh flex-col overflow-hidden bg-background">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b px-4">
        <Link to="/admin/semantic" className="flex items-center gap-2">
          <DatabaseIcon {...accent("semantic")} />
          <span className="text-sm font-semibold">Semantic Layer</span>
        </Link>
        <Link
          to="/admin/report-builder"
          className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
        >
          <FileSpreadsheetIcon {...accent("reports")} />
          Report Builder
        </Link>
        <span className="rounded-full border px-2 py-0.5 text-[11px] tracking-wide text-muted-foreground uppercase">
          Admin
        </span>

        {user?.role ? (
          <span
            className="truncate font-mono text-[11px] text-muted-foreground"
            title="Your active Snowflake role — it decides what you can edit"
          >
            {user.role}
          </span>
        ) : null}

        <a
          href="/"
          className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
        >
          Open AI Reporting
          <ExternalLinkIcon className="size-3.5" />
        </a>
      </header>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {/* The server refuses these routes anyway. This exists so a
            non-admin gets a sentence explaining why, rather than a page of
            failed requests that reads like the app is broken. */}
        {isLoading ? null : user && !user.is_admin ? (
          <div className="mx-auto max-w-md p-10 text-center">
            <ShieldAlertIcon className="mx-auto size-6 text-muted-foreground" />
            <h1 className="mt-3 text-lg font-medium">
              Administrator access required
            </h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Editing semantic layers is restricted to administrator roles. You
              are signed in as{" "}
              <span className="font-mono">{user.snowflake_user}</span> with the
              role <span className="font-mono">{user.role ?? "none"}</span>.
            </p>
            <a
              href="/"
              className="mt-4 inline-block text-sm underline underline-offset-4"
            >
              Back to AI Reporting
            </a>
          </div>
        ) : (
          <PageErrorBoundary>
            <Outlet />
          </PageErrorBoundary>
        )}
      </div>
    </div>
  )
}
