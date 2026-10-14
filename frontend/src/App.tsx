import { Navigate, Outlet, Route, Routes } from "react-router-dom"

import { AuthGuard } from "@/components/auth-guard"
import { ConversationsProvider } from "@/contexts/conversations-context"
import { DatabaseProvider } from "@/contexts/database-context"
import { AppShell } from "@/components/app-shell"
import { ExploreView } from "@/components/explore/explore-view"
import LoginPage from "@/pages/login"
import ChatPage from "@/pages/chat"
import ReportsPage from "@/pages/reports"
import DashboardsList from "@/pages/dashboards"
import DashboardDetail from "@/pages/dashboard-detail"
import ExcelSourcePage from "@/pages/excel-source"
import { AdminShell } from "@/components/admin/admin-shell"
import SemanticLayersPage from "@/pages/admin/semantic-layers"
import SemanticEditorPage from "@/pages/admin/semantic-editor"
import ReportBuilderPage from "@/pages/admin/report-builder"

function ProtectedShell() {
  return (
    <AuthGuard>
      <DatabaseProvider>
        <ConversationsProvider>
          <Outlet />
        </ConversationsProvider>
      </DatabaseProvider>
    </AuthGuard>
  )
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route element={<ProtectedShell />}>
        <Route element={<AppShell />}>
          <Route index element={<ChatPage />} />
          <Route path="explore" element={<ExploreView />} />
          {/* Excel as a data source. Sits in the main shell rather than under
              /admin because it is an analyst activity, not an authoring one —
              and its endpoint is open to any signed-in user, so gating the page
              on is_admin would misrepresent who can use it. */}
          <Route path="excel-source" element={<ExcelSourcePage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route path="reports/dashboards" element={<DashboardsList />} />
          <Route
            path="reports/dashboards/:reportId"
            element={<DashboardDetail />}
          />
          {/* Aliases: save_dashboard hands the user {base}/dashboards/{id}.
              Without these that path falls through to the catch-all below and
              silently redirects home. */}
          <Route path="dashboards" element={<DashboardsList />} />
          <Route path="dashboards/:reportId" element={<DashboardDetail />} />
        </Route>

        {/* Admin sits outside AppShell: it opens in its own tab and is a
            different job from asking questions of data, so it gets its own
            chrome rather than the chat sidebar. Still inside ProtectedShell —
            authentication is not optional here. */}
        <Route path="admin" element={<AdminShell />}>
          <Route path="semantic" element={<SemanticLayersPage />} />
          <Route path="semantic/:database" element={<SemanticEditorPage />} />
          <Route path="report-builder" element={<ReportBuilderPage />} />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

export default App
