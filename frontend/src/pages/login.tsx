/**
 * The way in to the product.
 *
 * This used to be a bare card on an empty page, which meant the first thing a
 * new user saw was a Snowflake consent screen with no idea what they were
 * granting access to. The left panel exists to answer "what is this and why is
 * it asking" before the click, not to sell.
 *
 * The sign-in side renders whatever /auth/capabilities reports, resolved
 * through lib/auth-methods. Nothing here knows that Snowflake is the current
 * provider — when identity moves to a database, this page picks the new method
 * up without being edited.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { ArrowRightIcon, Loader2Icon, TriangleAlertIcon } from "lucide-react"

import { useAuth } from "@/contexts/auth-context"
import { BrandLockup } from "@/components/brand"
import { ShowcaseCarousel } from "@/components/showcase-carousel"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { api, fetchAuthCapabilities, type AuthCapabilities } from "@/lib/api"
import { availableMethods, type AuthMethod } from "@/lib/auth-methods"
import { cn } from "@/lib/utils"

export default function LoginPage() {
  const { user, login, refreshUser } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const [capabilities, setCapabilities] = useState<AuthCapabilities | null>(null)
  const [probeFailed, setProbeFailed] = useState(false)
  const [pending, setPending] = useState<string | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})

  /* The OAuth callback reports failures by redirecting back with ?error=. */
  const redirectError = searchParams.get("error")

  useEffect(() => {
    if (user) navigate("/", { replace: true })
  }, [user, navigate])

  useEffect(() => {
    void fetchAuthCapabilities()
      .then(setCapabilities)
      .catch(() => setProbeFailed(true))
  }, [])

  const methods = useMemo(() => availableMethods(capabilities), [capabilities])
  /* Lowest order leads; anything else real is still offered, just not as the
     filled button. A deployment running password *and* SSO during a migration
     has to show both. */
  const [primary, ...alternates] = methods.filter((m) => !m.secondary)
  const secondary = methods.filter((m) => m.secondary)

  const run = useCallback(
    async (method: AuthMethod) => {
      setFormError(null)

      if (method.kind === "redirect") {
        login()
        return
      }

      setPending(method.id)
      try {
        await api(method.endpoint!, {
          method: "POST",
          json: method.kind === "credentials" ? values : undefined,
        })
        await refreshUser()
      } catch (err) {
        setFormError(err instanceof Error ? err.message : "Sign-in failed")
      } finally {
        setPending(null)
      }
    },
    [login, refreshUser, values]
  )

  return (
    <div className="grid min-h-svh lg:grid-cols-[1.1fr_1fr]">
      {/* ---------------------------------------------------------- brand */}
      {/* Hidden below lg rather than stacked: on a phone it would push the
          thing people came for below the fold.

          Fixed dark colours, not bg-foreground/text-background: those invert
          with the theme, which turned the panel white in dark mode. */}
      <aside className="relative hidden overflow-hidden bg-neutral-950 p-12 text-neutral-50 lg:flex lg:flex-col lg:justify-between lg:border-r lg:border-white/10">
        {/* A quiet field of light so the panel is not a flat slab. */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-[0.16]"
          style={{
            backgroundImage:
              "radial-gradient(60rem 40rem at 15% 10%, #6366F1 0%, transparent 60%), radial-gradient(48rem 34rem at 90% 85%, #1D4ED8 0%, transparent 62%)",
          }}
        />

        {/* The full lockup, tagline and all, because this is the one surface
            with the width to carry it. Knockout artwork — the wordmark is
            near-black and vanishes against this panel. */}
        <div className="relative">
          <BrandLockup variant="full" tone="inverse" className="w-[15rem]" />
        </div>

        <div className="relative max-w-xl">
          <h1 className="text-4xl leading-[1.15] font-semibold tracking-tight text-balance">
            Ask your data a question.
          </h1>
          <p className="mt-4 text-base text-white/60">
            Answers, dashboards and reports from your warehouse and your
            own files.
          </p>

          <ShowcaseCarousel className="mt-10" />
        </div>

        <p className="relative text-xs text-white/40">
          Runs in your own cloud account.
        </p>
      </aside>

      {/* ------------------------------------------------------- sign in */}
      <main className="flex items-center justify-center px-6 py-12">
        <div className="w-full max-w-sm">
          {/* The logo repeats here because the brand panel is hidden on
              phones, and an unbranded login form is a phishing lesson. The
              compact lockup, since a phone has no room for the tagline. */}
          <div className="mb-9 lg:hidden">
            <BrandLockup className="w-[9.5rem]" />
          </div>

          <h2 className="text-2xl font-semibold tracking-tight">Sign in</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            {primary?.description ??
              "Use the account your organisation has given you."}
          </p>

          {redirectError ? (
            <Notice tone="error">Sign-in failed: {redirectError}</Notice>
          ) : null}
          {formError ? <Notice tone="error">{formError}</Notice> : null}

          <div className="mt-7 space-y-4">
            {capabilities === null && !probeFailed ? (
              <div className="h-10 animate-pulse rounded-md bg-muted" />
            ) : null}

            {probeFailed ? (
              <Notice tone="warn">
                Could not reach the server to check which sign-in methods are
                available. Refresh, or contact your administrator if this
                persists.
              </Notice>
            ) : null}

            {capabilities !== null && methods.length === 0 ? (
              <Notice tone="warn">
                No sign-in method is configured on this deployment yet. An
                administrator needs to finish setting up authentication.
              </Notice>
            ) : null}

            {primary ? (
              <MethodForm
                method={primary}
                values={values}
                onChange={(name, value) =>
                  setValues((v) => ({ ...v, [name]: value }))
                }
                onSubmit={() => run(primary)}
                pending={pending === primary.id}
              />
            ) : null}

            {alternates.length > 0 ? (
              <div className="relative py-2 text-center">
                <span className="absolute inset-x-0 top-1/2 border-t" />
                <span className="relative bg-background px-3 text-xs text-muted-foreground">
                  or
                </span>
              </div>
            ) : null}

            {alternates.map((method) => (
              <div key={method.id} className="space-y-2">
                <MethodForm
                  method={method}
                  variant="outline"
                  values={values}
                  onChange={(name, value) =>
                    setValues((v) => ({ ...v, [name]: value }))
                  }
                  onSubmit={() => run(method)}
                  pending={pending === method.id}
                />
                {method.description ? (
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    {method.description}
                  </p>
                ) : null}
              </div>
            ))}
          </div>

          {secondary.length > 0 ? (
            <div className="mt-8 border-t pt-6">
              {secondary.map((method) => (
                <div key={method.id} className="space-y-2">
                  <Button
                    variant="outline"
                    className="w-full"
                    onClick={() => void run(method)}
                    disabled={pending !== null}
                  >
                    {pending === method.id ? (
                      <Loader2Icon className="size-4 animate-spin" />
                    ) : null}
                    {method.label}
                  </Button>
                  {method.description ? (
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      {method.description}
                    </p>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}

          <p className="mt-10 text-xs text-muted-foreground">
            Access is provisioned by your administrator.
          </p>
        </div>
      </main>
    </div>
  )
}

/**
 * One method, drawn according to its kind.
 *
 * A credentials method is a real form — submit on Enter, and the browser's
 * password manager can see the fields, which it cannot if the inputs are
 * assembled outside a <form>.
 */
function MethodForm({
  method,
  values,
  onChange,
  onSubmit,
  pending,
  variant = "default",
}: {
  method: AuthMethod
  values: Record<string, string>
  onChange: (name: string, value: string) => void
  onSubmit: () => void
  pending: boolean
  variant?: "default" | "outline"
}) {
  const button = (
    <Button
      type={method.kind === "credentials" ? "submit" : "button"}
      size="lg"
      variant={variant}
      className="w-full gap-2"
      disabled={pending}
      onClick={method.kind === "credentials" ? undefined : onSubmit}
    >
      {pending ? <Loader2Icon className="size-4 animate-spin" /> : null}
      {method.label}
      {!pending && method.kind === "redirect" ? (
        <ArrowRightIcon className="size-4" />
      ) : null}
    </Button>
  )

  if (method.kind !== "credentials") return button

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit()
      }}
    >
      {(method.fields ?? []).map((field) => (
        <div key={field.name} className="space-y-1.5">
          <Label htmlFor={field.name}>{field.label}</Label>
          <Input
            id={field.name}
            name={field.name}
            type={field.type}
            autoComplete={field.autoComplete}
            placeholder={field.placeholder}
            value={values[field.name] ?? ""}
            onChange={(e) => onChange(field.name, e.target.value)}
          />
        </div>
      ))}
      {button}
    </form>
  )
}

function Notice({
  tone,
  children,
}: {
  tone: "error" | "warn"
  children: ReactNode
}) {
  return (
    <p
      className={cn(
        "mt-5 flex items-start gap-2 rounded-lg border p-3 text-xs leading-relaxed",
        tone === "error"
          ? "border-destructive/40 bg-destructive/5 text-destructive"
          : "border-amber-500/40 bg-amber-500/5 text-amber-700 dark:text-amber-500"
      )}
    >
      <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
      <span>{children}</span>
    </p>
  )
}
