/**
 * One HTTP client for the whole app.
 *
 * It knows three things nobody else has to: the response envelope, that a 401 means
 * "try the refresh cookie once", and that concurrent requests during a refresh should
 * wait for the same refresh rather than each starting their own.
 */

const BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
const PREFIX = '/api/v1'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
    readonly traceId?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

interface Envelope<T> {
  data: T
  meta?: { next_cursor?: string; has_more?: boolean }
}

let accessToken: string | null = null
let onUnauthenticated: (() => void) | null = null
let refreshInFlight: Promise<boolean> | null = null

export const auth = {
  set(token: string | null) {
    accessToken = token
  },
  get() {
    return accessToken
  },
  onLogout(handler: () => void) {
    onUnauthenticated = handler
  },
}

export function apiUrl(path: string): string {
  return `${BASE}${PREFIX}${path}`
}

async function parse<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T
  const body = await response.json().catch(() => null)

  if (!response.ok) {
    const error = body?.error ?? {}
    throw new ApiError(
      response.status,
      error.code ?? 'http_error',
      error.message ?? `Request failed (${response.status})`,
      error.details ?? {},
      error.trace_id,
    )
  }
  return (body as Envelope<T>).data
}

async function refreshOnce(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(apiUrl('/auth/refresh'), {
          method: 'POST',
          credentials: 'include',
        })
        if (!response.ok) return false
        const body = await response.json()
        accessToken = body.data.access_token
        return true
      } catch {
        return false
      } finally {
        // Release the gate on the next tick so queued callers see the new token.
        setTimeout(() => (refreshInFlight = null), 0)
      }
    })()
  }
  return refreshInFlight
}

interface RequestOptions {
  method?: string
  body?: unknown
  retryOn401?: boolean
  signal?: AbortSignal
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, retryOn401 = true, signal } = options

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`

  const response = await fetch(apiUrl(path), {
    method,
    headers,
    credentials: 'include',
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  if (response.status === 401 && retryOn401) {
    if (await refreshOnce()) {
      return request<T>(path, { ...options, retryOn401: false })
    }
    auth.set(null)
    onUnauthenticated?.()
  }

  return parse<T>(response)
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { method: 'GET', signal }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  refresh: refreshOnce,
}
