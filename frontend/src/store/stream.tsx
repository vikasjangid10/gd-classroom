import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from 'react'
import { useLocation } from 'react-router-dom'
import { api } from '@/lib/api'
import { useEventStream, type StreamStatus } from '@/hooks/useEventStream'
import type { ServerEvent } from '@/types'

type Listener = (event: ServerEvent) => void

interface StreamValue {
  status: StreamStatus
  subscribe: (listener: Listener) => () => void
}

const StreamContext = createContext<StreamValue | null>(null)

const ROOM_PATH = /^\/room\/([0-9a-fA-F-]{36})/

/**
 * Exactly one Server-Sent Events connection per tab.
 *
 * Browsers allow six concurrent HTTP/1.1 connections per origin, and they are shared
 * across tabs of the same site. Opening a lobby stream *and* a room stream per tab
 * exhausted that budget with three tabs open, at which point every subsequent request —
 * including the one that submits your turn — queued behind the streams and never went
 * out. So the tab keeps a single connection and simply changes which endpoint it points
 * at: the room endpoint already carries the user's own topic as well as the session's.
 */
export function StreamProvider({
  children,
  enabled,
}: {
  children: ReactNode
  enabled: boolean
}) {
  const { pathname } = useLocation()
  const sessionId = ROOM_PATH.exec(pathname)?.[1] ?? null
  const listeners = useRef(new Set<Listener>())

  const path = sessionId ? `/sessions/${sessionId}/events` : '/events'

  const getTicket = useCallback(async () => {
    if (sessionId) {
      const tickets = await api.post<{ sse_ticket: string }>(`/sessions/${sessionId}/tickets`)
      return tickets.sse_ticket
    }
    return (await api.post<{ ticket: string }>('/me/stream-ticket')).ticket
  }, [sessionId])

  const onEvent = useCallback((event: ServerEvent) => {
    listeners.current.forEach((listener) => listener(event))
  }, [])

  const { status } = useEventStream({ path, getTicket, enabled, onEvent })

  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener)
    return () => {
      listeners.current.delete(listener)
    }
  }, [])

  const value = useMemo(() => ({ status, subscribe }), [status, subscribe])
  return <StreamContext.Provider value={value}>{children}</StreamContext.Provider>
}

export function useStream(): StreamValue {
  const context = useContext(StreamContext)
  if (!context) throw new Error('useStream must be used inside <StreamProvider>')
  return context
}

/** Register a handler for every event on the tab's stream. */
export function useStreamEvents(handler: Listener): StreamStatus {
  const { status, subscribe } = useStream()
  const ref = useRef(handler)
  ref.current = handler

  useEffect(() => subscribe((event) => ref.current(event)), [subscribe])
  return status
}
