import * as React from "react"
import { useLocation } from "react-router-dom"

import { Button } from "@/components/ui/button"

type FallbackProps = { error: Error; reset: () => void }

type ErrorBoundaryProps = {
  children: React.ReactNode
  fallback: (props: FallbackProps) => React.ReactNode
  /** A caught error is cleared when this changes — new data, a new page. */
  resetKey?: unknown
  /** Names the boundary in the console, so a report says which part failed. */
  label?: string
}

type ErrorBoundaryState = { error: Error | null }

/**
 * Contains a render error to the part of the screen that threw it.
 *
 * Without one, React unmounts the whole tree on any render error: a single
 * chart choking on an unexpected value blanked the entire dashboard, and a
 * reader saw an empty page with no hint that the other nineteen panels were
 * fine. React still only offers this as a class component.
 */
export class ErrorBoundary extends React.Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(
      `[${this.props.label ?? "error boundary"}]`,
      error,
      info.componentStack
    )
  }

  componentDidUpdate(previous: ErrorBoundaryProps) {
    if (this.state.error && !Object.is(previous.resetKey, this.props.resetKey)) {
      this.setState({ error: null })
    }
  }

  reset = () => this.setState({ error: null })

  render() {
    if (this.state.error) {
      return this.props.fallback({ error: this.state.error, reset: this.reset })
    }
    return this.props.children
  }
}

/**
 * One panel's boundary. Keyed on the panel itself, so a refresh that brings new
 * data gives a failed chart another chance instead of leaving it blank.
 */
export function PanelErrorBoundary({
  title,
  resetKey,
  children,
}: {
  title?: string | null
  resetKey?: unknown
  children: React.ReactNode
}) {
  return (
    <ErrorBoundary
      label={`panel: ${title || "untitled"}`}
      resetKey={resetKey}
      fallback={() => (
        <div
          role="alert"
          className="flex h-full min-h-32 flex-col items-center justify-center gap-1 rounded-xl border border-dashed border-border p-4 text-center"
        >
          <p className="text-sm font-medium text-foreground">
            This chart couldn&apos;t be displayed
          </p>
          <p className="text-xs text-muted-foreground">
            {title ? `“${title}” hit a display error. ` : ""}The rest of the
            dashboard is unaffected.
          </p>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  )
}

/**
 * The page's boundary, inside the app shell so the sidebar survives. Reset on
 * navigation: moving to another page should not carry the last page's error.
 */
export function PageErrorBoundary({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation()
  return (
    <ErrorBoundary
      label={`page: ${pathname}`}
      resetKey={pathname}
      fallback={({ reset }) => (
        <div
          role="alert"
          className="mx-auto flex max-w-md flex-col items-center gap-3 px-6 py-24 text-center"
        >
          <p className="text-base font-medium text-foreground">
            Something went wrong on this page
          </p>
          <p className="text-sm text-muted-foreground">
            Nothing was lost. Try again, or reload the page if it keeps
            happening.
          </p>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={reset}>
              Try again
            </Button>
            <Button size="sm" onClick={() => window.location.reload()}>
              Reload page
            </Button>
          </div>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  )
}
