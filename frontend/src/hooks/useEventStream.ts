import { useEffect, useRef, useState } from 'react'
import { apiUrl } from '@/lib/api'
import type { ServerEvent } from '@/types'

export type StreamStatus = 'idle' | 'connecting' | 'open' | 'retrying' | 'failed'

interface Options {
  /** Fetches a fresh short-lived ticket. Called before every connect attempt. */
  getTicket: () => Promise<string>
  path: string
  enabled?: boolean
  onEvent: (event: ServerEvent) => void
}

const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000]

/**
 * A resilient EventSource.
 *
 * Three things make this more than a one-liner:
 *  - tickets expire in 60 s, so every reconnect fetches a new one;
 *  - `Last-Event-ID` is tracked manually and sent as a query parameter, because a
 *    reconnect uses a brand new EventSource and the browser will not resend it for us;
 *  - the server may ask for a full resync (`stream.resync`) when the client has fallen
 *    outside the replay window.
 */
export function useEventStream({ getTicket, path, enabled = true, onEvent }: Options) {
  const [status, setStatus] = useState<StreamStatus>('idle')
  const lastSeq = useRef(0)
  const attempt = useRef(0)
  const handler = useRef(onEvent)
  handler.current = onEvent

  useEffect(() => {
    if (!enabled) {
      setStatus('idle')
      return
    }

    let source: EventSource | null = null
    let timer: number | undefined
    let disposed = false

    const connect = async () => {
      if (disposed) return
      setStatus(attempt.current === 0 ? 'connecting' : 'retrying')

      let ticket: string
      try {
        ticket = await getTicket()
      } catch {
        schedule()
        return
      }
      if (disposed) return

      const url = new URL(apiUrl(path))
      url.searchParams.set('ticket', ticket)
      if (lastSeq.current > 0) url.searchParams.set('last_event_id', String(lastSeq.current))

      source = new EventSource(url.toString())

      source.onopen = () => {
        attempt.current = 0
        setStatus('open')
      }

      source.onerror = () => {
        source?.close()
        source = null
        if (!disposed) schedule()
      }

      source.onmessage = (message) => dispatch(message)
      // Named events do not reach onmessage, and the server names every frame.
      const named = [
        'stream.open',
        'stream.resync',
        'error',
        'classroom.updated',
        'invitation.sent',
        'invitation.responded',
        'session.ready',
        'session.state',
        'participant.connected',
        'participant.disconnected',
        'moderator.speaking',
        'moderator.interrupted',
        'floor.granted',
        'floor.released',
        'transcript.partial',
        'transcript.final',
        'speaking_time.updated',
        'session.summary_ready',
        'session.ended',
      ]
      named.forEach((name) => source?.addEventListener(name, dispatch as EventListener))
    }

    const dispatch = (message: MessageEvent) => {
      if (message.type === 'stream.resync') {
        lastSeq.current = 0
        handler.current({
          v: 1,
          seq: 0,
          ts: new Date().toISOString(),
          type: 'stream.resync',
          topic: '',
          payload: {},
        })
        return
      }
      try {
        const parsed = JSON.parse(message.data) as ServerEvent
        if (typeof parsed.seq === 'number' && parsed.seq > 0) {
          if (parsed.seq <= lastSeq.current) return // duplicate from replay
          lastSeq.current = parsed.seq
        }
        handler.current(parsed)
      } catch {
        /* a malformed frame must not tear down the stream */
      }
    }

    const schedule = () => {
      const delay = BACKOFF_MS[Math.min(attempt.current, BACKOFF_MS.length - 1)]
      attempt.current += 1
      if (attempt.current > 40) {
        setStatus('failed')
        return
      }
      setStatus('retrying')
      timer = window.setTimeout(connect, delay + Math.random() * 250)
    }

    void connect()

    return () => {
      disposed = true
      if (timer) window.clearTimeout(timer)
      source?.close()
      setStatus('idle')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, enabled])

  return { status, resetCursor: () => (lastSeq.current = 0) }
}
