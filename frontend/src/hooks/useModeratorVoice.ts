/**
 * Plays the moderator's voice for people who are not on WebRTC.
 *
 * Joining the media plane needs a microphone, and plenty of people open a discussion on
 * a laptop without one. They still have to *hear* the host — a silent moderator with a
 * caption scrolling past is not a spoken group discussion.
 *
 * The server publishes one `moderator.audio` event per synthesised sentence. This queues
 * them and plays them strictly in order: sentences arrive while the previous one is
 * still playing, and playing them concurrently would turn the moderator into a chorus.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiUrl, auth } from '@/lib/api'

export interface VoiceState {
  /** True while a clip is actually audible. */
  speaking: boolean
  /** Set when the browser refused to autoplay, so the UI can ask for one click. */
  blocked: boolean
  /** Call from a click handler to satisfy the autoplay policy. */
  enable: () => void
  /** Queue one synthesised sentence, from a `moderator.audio` event. */
  push: (clipId: string) => void
  /** Drop everything queued — barge-in, or the discussion ending. */
  stop: () => void
}

export function useModeratorVoice(sessionId: string, enabled: boolean): VoiceState {
  const [speaking, setSpeaking] = useState(false)
  const [blocked, setBlocked] = useState(false)

  const queue = useRef<string[]>([])
  const playing = useRef(false)
  const element = useRef<HTMLAudioElement | null>(null)
  const objectUrl = useRef<string | null>(null)

  if (element.current === null && typeof Audio !== 'undefined') {
    element.current = new Audio()
  }

  const release = useCallback(() => {
    if (objectUrl.current) {
      URL.revokeObjectURL(objectUrl.current)
      objectUrl.current = null
    }
  }, [])

  const drain = useCallback(async () => {
    if (playing.current) return
    playing.current = true
    try {
      while (queue.current.length > 0) {
        const clipId = queue.current.shift()!
        const audio = element.current
        if (!audio) break

        try {
          // Bearer rather than a stream ticket: unlike EventSource, fetch can set
          // headers, and this is an ordinary download.
          const response = await fetch(apiUrl(`/sessions/${sessionId}/speech/${clipId}`), {
            headers: auth.get() ? { Authorization: `Bearer ${auth.get()}` } : {},
            credentials: 'include',
          })
          // 404 means the clip aged out of the server's small cache. Skipping a
          // sentence is far better than stalling the whole queue behind it.
          if (!response.ok) continue

          release()
          objectUrl.current = URL.createObjectURL(await response.blob())
          audio.src = objectUrl.current

          setSpeaking(true)
          await audio.play()
          await new Promise<void>((resolve) => {
            audio.onended = () => resolve()
            audio.onerror = () => resolve()
          })
          setBlocked(false)
        } catch {
          // Autoplay policy, or the tab lost audio focus. Keep the remaining clips —
          // one click on "Turn on sound" resumes from wherever the moderator is now.
          setBlocked(true)
        }
      }
    } finally {
      playing.current = false
      setSpeaking(false)
    }
  }, [sessionId, release])

  const push = useCallback(
    (clipId: string) => {
      if (!enabled) return
      queue.current.push(clipId)
      void drain()
    },
    [enabled, drain],
  )

  const enable = useCallback(() => {
    setBlocked(false)
    void drain()
  }, [drain])

  // Barge-in and session end: stop mid-sentence rather than talking over a participant.
  const stop = useCallback(() => {
    queue.current.length = 0
    const audio = element.current
    if (audio) {
      audio.pause()
      audio.currentTime = 0
    }
    setSpeaking(false)
  }, [])

  useEffect(() => {
    return () => {
      stop()
      release()
    }
  }, [stop, release])

  return { speaking, blocked, enable, push, stop }
}
