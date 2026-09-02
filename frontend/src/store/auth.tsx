import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, auth as tokenStore } from '@/lib/api'
import type { Gender, Role, User } from '@/types'

interface AuthState {
  user: User | null
  loading: boolean
  login: (email: string, password: string) => Promise<User>
  register: (input: {
    email: string
    password: string
    display_name: string
    role: Role
    gender: Gender
  }) => Promise<void>
  /** Adopt a session someone obtained without a password — an accepted email invitation. */
  adopt: (user: User) => void
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

interface LoginResponse {
  access_token: string
  expires_in: number
  user: User
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  // The refresh token lives in an httpOnly cookie, so a page reload can restore the
  // session without ever having stored an access token in localStorage.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const restored = await api.refresh()
      if (restored && !cancelled) {
        try {
          setUser(await api.get<User>('/users/me'))
        } catch {
          tokenStore.set(null)
        }
      }
      if (!cancelled) setLoading(false)
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    tokenStore.onLogout(() => setUser(null))
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const result = await api.post<LoginResponse>('/auth/login', { email, password })
    tokenStore.set(result.access_token)
    setUser(result.user)
    return result.user
  }, [])

  const register = useCallback(
    async (input: {
      email: string
      password: string
      display_name: string
      role: Role
      gender: Gender
    }) => {
      await api.post('/auth/register', input)
    },
    [],
  )

  const adopt = useCallback((next: User) => setUser(next), [])

  const logout = useCallback(async () => {
    try {
      await api.post('/auth/logout')
    } finally {
      tokenStore.set(null)
      setUser(null)
    }
  }, [])

  const value = useMemo(
    () => ({ user, loading, login, register, adopt, logout }),
    [user, loading, login, register, adopt, logout],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}
