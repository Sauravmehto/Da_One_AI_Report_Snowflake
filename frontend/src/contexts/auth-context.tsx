import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react"

import {
  fetchAvailableRoles,
  fetchCurrentUser,
  getLoginUrl,
  logout as apiLogout,
  onUnauthorized,
  setActiveRole,
  type AuthUser,
} from "@/lib/api"

type AuthContextValue = {
  user: AuthUser | null
  isLoading: boolean
  availableRoles: string[]
  login: () => void
  logout: () => Promise<void>
  refreshUser: () => Promise<void>
  setRole: (role: string) => Promise<void>
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [availableRoles, setAvailableRoles] = useState<string[]>([])

  const refreshUser = useCallback(async () => {
    try {
      const currentUser = await fetchCurrentUser()
      setUser(currentUser)
      try {
        const roles = await fetchAvailableRoles()
        setAvailableRoles(roles.roles)
      } catch {
        setAvailableRoles([])
      }
    } catch {
      setUser(null)
      setAvailableRoles([])
    } finally {
      setIsLoading(false)
    }
  }, [])

  // Track whether the user is considered logged in so we can avoid redundant
  // redirects when multiple 401s fire at once.
  const isLoggedInRef = useRef(false)
  useEffect(() => {
    isLoggedInRef.current = user !== null
  }, [user])

  useEffect(() => {
    void refreshUser()
  }, [refreshUser])

  // Periodic session validity check every 5 minutes
  useEffect(() => {
    const id = setInterval(() => {
      void refreshUser()
    }, 5 * 60 * 1000)
    return () => clearInterval(id)
  }, [refreshUser])

  // Global 401 intercept — when any API call returns 401, clear the session
  // immediately so the auth guard redirects to login.
  useEffect(() => {
    const unsub = onUnauthorized(() => {
      if (isLoggedInRef.current) {
        setUser(null)
        setAvailableRoles([])
        isLoggedInRef.current = false
      }
    })
    return unsub
  }, [])

  const login = useCallback(() => {
    window.location.href = getLoginUrl()
  }, [])

  const logout = useCallback(async () => {
    await apiLogout()
    setUser(null)
    setAvailableRoles([])
  }, [])

  const setRole = useCallback(async (role: string) => {
    const updated = await setActiveRole(role)
    setUser(updated)
  }, [])

  const value = useMemo(
    () => ({
      user,
      isLoading,
      availableRoles,
      login,
      logout,
      refreshUser,
      setRole,
    }),
    [user, isLoading, availableRoles, login, logout, refreshUser, setRole]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider")
  }
  return context
}
