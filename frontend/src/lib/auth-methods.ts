/**
 * The sign-in methods this deployment can offer, and how to render each one.
 *
 * WHY A REGISTRY RATHER THAN BUTTONS IN THE PAGE
 * Today there is exactly one real method: Snowflake OAuth. Identity is expected
 * to move — to a users table in a SQL database, or to an enterprise IdP — and
 * when it does, the login page should not need rewriting. So the page renders
 * whatever the SERVER says it supports: it asks /auth/capabilities, looks each
 * returned key up here, and draws the result.
 *
 * Adding password sign-in later is therefore two changes and no new UI:
 *
 *   1. the backend returns `password: true` from /auth/capabilities and exposes
 *      the endpoint named below;
 *   2. an entry is added to METHODS with kind "credentials" and its fields.
 *
 * A capability the server reports but this file does not know about is ignored
 * rather than rendered blank — an older frontend against a newer backend shows
 * the methods it understands instead of an empty card.
 */

export type CredentialField = {
  name: string
  label: string
  type: "text" | "password" | "email"
  autoComplete?: string
  placeholder?: string
}

export type AuthMethod = {
  /** Must match the key returned by /auth/capabilities. */
  id: string
  label: string
  /**
   * redirect     — hand off to an external identity provider
   * action       — POST with no input from the user
   * credentials  — collect fields here and POST them
   */
  kind: "redirect" | "action" | "credentials"
  /** One line under the button. Omit when the label says enough. */
  description?: string
  /** Lowest order among the available methods becomes the primary button. */
  order: number
  /** Drawn quietly, below a divider. For anything that is not a real login. */
  secondary?: boolean
  /** credentials only. */
  fields?: CredentialField[]
  /** credentials and action only — the path to POST to. */
  endpoint?: string
}

export const METHODS: AuthMethod[] = [
  {
    id: "oauth",
    kind: "redirect",
    label: "Continue with Snowflake",
    description: "You sign in to Snowflake. We never see your password.",
    order: 10,
  },
  {
    id: "dev_login",
    kind: "action",
    endpoint: "/auth/dev-login",
    label: "Developer sign-in",
    description:
      "Local development only — uses the shared service account, not your own identity.",
    order: 90,
    secondary: true,
  },

  // When identity moves to a database, this is the whole change:
  //
  // {
  //   id: "password",
  //   kind: "credentials",
  //   endpoint: "/auth/password",
  //   label: "Sign in",
  //   order: 5,
  //   fields: [
  //     { name: "email", label: "Work email", type: "email",
  //       autoComplete: "username", placeholder: "you@company.com" },
  //     { name: "password", label: "Password", type: "password",
  //       autoComplete: "current-password" },
  //   ],
  // },
]

/**
 * The methods to draw, in display order.
 *
 * `capabilities` is whatever /auth/capabilities returned. Unknown keys are
 * dropped here rather than at the call site, so the page only ever sees methods
 * it knows how to render.
 */
export function availableMethods(
  capabilities: Record<string, boolean> | null
): AuthMethod[] {
  if (!capabilities) return []
  return METHODS.filter((method) => capabilities[method.id] === true).sort(
    (a, b) => a.order - b.order
  )
}
