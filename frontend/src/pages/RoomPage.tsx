import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ApiError, api } from '@/lib/api'
import { useStreamEvents } from '@/store/stream'
import { useRoom } from '@/hooks/useRoom'
import { useWebRTC } from '@/hooks/useWebRTC'
import { useModeratorVoice } from '@/hooks/useModeratorVoice'
import { OnAirDot } from '@/components/Layout'
import { ErrorNote, SessionBadge, formatDuration } from '@/components/ui'
import { useAuth } from '@/store/auth'
import type { ServerEvent, SessionInfo, Tickets } from '@/types'

/**
 * What goes in the avatar circle.
 *
 * Call-signs share a word — "Speaker 1", "Speaker 2" — so the first two letters make
 * every tile read "SP". The number is the part that distinguishes them.
 */
function initials(name: string): string {
  const last = name.trim().split(/\s+/).pop() ?? ''
  if (/^\d+$/.test(last)) return last
  return name.slice(0, 2).toUpperCase()
}

function ThinkingDots() {
  return (
    <span className="inline-flex gap-1">
      {[0, 150, 300].map((delay) => (
        <span
          key={delay}
          className="h-1.5 w-1.5 animate-bounce rounded-full bg-signal"
          style={{ animationDelay: `${delay}ms` }}
        />
      ))}
    </span>
  )
}

export default function RoomPage() {
  const { sessionId = '' } = useParams()
  const navigate = useNavigate()
  const { user } = useAuth()

  const { room, onEvent: reduceEvent, hydrate } = useRoom()
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [textAllowed, setTextAllowed] = useState(true)
  const [joinedByText, setJoinedByText] = useState(false)
  const transcriptEnd = useRef<HTMLDivElement>(null)

  const getTickets = useCallback(
    () => api.post<Tickets>(`/sessions/${sessionId}/tickets`),
    [sessionId],
  )

  const rtc = useWebRTC({ sessionId, getTickets })
  // On WebRTC the moderator already arrives as live audio. Playing the clips as well
  // would make it say everything twice, a beat apart.
  const voice = useModeratorVoice(sessionId, rtc.status !== 'connected')

  const onEvent = useCallback(
    (event: ServerEvent) => {
      if (event.type === 'moderator.audio') {
        voice.push(String((event.payload as { clip_id: string }).clip_id))
      } else if (event.type === 'moderator.interrupted' || event.type === 'session.ended') {
        voice.stop()
      }
      reduceEvent(event)
    },
    [voice, reduceEvent],
  )

  // The tab's single stream is already pointed at this room (see StreamProvider).
  const streamStatus = useStreamEvents(onEvent)

  // Hydrate from the durable record + live snapshot, so a refresh mid-discussion
  // shows the correct state before the first event arrives.
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    ;(async () => {
      try {
        const info = await api.get<SessionInfo>(`/sessions/${sessionId}`)
        if (cancelled) return
        setSession(info)
        hydrate({
          sessionStatus: info.live?.status ?? info.status,
          floorHolder: info.live?.floor_holder ?? null,
          speakingTime: info.live?.speaking_time ?? [],
          connected: info.live?.connected ?? [],
          moderatorState: info.live?.moderator_state ?? 'IDLE',
          ended: ['ENDED', 'ABORTED'].includes(info.status),
        })
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : 'Could not open this discussion.')
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionId, hydrate])

  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [room.transcript.length, room.partial?.text])

  const roster = useMemo(() => {
    if (room.speakingTime.length > 0) return room.speakingTime
    return (session?.participants ?? []).map((p) => ({
      participant_id: p.user_id,
      // Your call-sign, not your account name — including your own card. Seeing "Priya"
      // on your own tile while everyone else sees "Speaker 3" would make it look like
      // the anonymity is cosmetic.
      display_name: p.display_name || 'Participant',
      seconds: Math.round(p.spoken_ms / 1000),
      turns: p.turns_taken,
      connected: room.connected.includes(p.user_id),
    }))
  }, [room.speakingTime, room.connected, session, user])

  const maxSeconds = Math.max(60, ...roster.map((r) => r.seconds))
  const iHaveFloor = room.floorHolder === user?.id
  const iWasRemoved = Boolean(user && room.removed.includes(user.id))

  // Being removed means removed: drop the microphone rather than leaving it open on a
  // page that is no longer part of the discussion.
  useEffect(() => {
    if (iWasRemoved) rtc.disconnect()
  }, [iWasRemoved, rtc])

  // Derived from what the room can see, rather than from a state name the server
  // would have to broadcast on every internal transition.
  const moderatorActivity = room.ended
    ? 'finished'
    : room.moderatorSpeaking || voice.speaking
      ? 'speaking'
      : room.moderatorThinking
        ? 'considering that'
        : room.floorHolder
          ? 'listening'
          : room.sessionStatus === 'ACTIVE'
            ? 'thinking'
            : 'waiting'

  const sendText = async (event: FormEvent) => {
    event.preventDefault()
    if (!text.trim()) return
    try {
      await api.post(`/sessions/${sessionId}/turn-text`, { text: text.trim() })
      setText('')
    } catch (err) {
      if (err instanceof ApiError && err.code === 'conflict') setTextAllowed(false)
      setError(err instanceof ApiError ? err.message : 'Could not send that.')
    }
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_340px]">
      <audio ref={rtc.audioRef} autoPlay playsInline className="hidden" />

      {/* ------------------------------------------------------------ stage */}
      <div className="space-y-5">
        <header className="flex flex-wrap items-center gap-3">
          <OnAirDot active={room.sessionStatus === 'ACTIVE'} />
          <h1 className="text-lg font-bold tracking-tight">Discussion room</h1>
          <SessionBadge status={room.sessionStatus} />
          {streamStatus !== 'open' && (
            <span className="pill border-amber-200 bg-amber-50 text-amber-700">
              {streamStatus === 'retrying' ? 'reconnecting' : streamStatus}
            </span>
          )}
        </header>

        <ErrorNote message={error ?? rtc.error} />

        {voice.blocked && (
          <button
            onClick={voice.enable}
            className="flex w-full items-center gap-3 rounded-xl border border-signal/40 bg-signal-soft px-4 py-3 text-left transition hover:bg-signal/10"
          >
            <span className="label text-signal-deep">Sound is off</span>
            <span className="text-sm font-semibold">
              Your browser blocked autoplay — tap to hear the host
            </span>
            <span className="ml-auto text-signal">Turn on →</span>
          </button>
        )}

        {/* moderator */}
        <section className="card relative overflow-hidden p-5">
          <div className="flex items-start gap-4">
            <div className="relative flex h-12 w-12 flex-none items-center justify-center rounded-full bg-signal-soft text-signal">
              {(room.moderatorSpeaking || voice.speaking) && (
                <span className="absolute inset-0 animate-pulse-ring rounded-full bg-signal/30" />
              )}
              <span className="font-mono text-[11px] font-bold">AI</span>
            </div>
            <div className="min-w-0 flex-1">
              <div className="label">Host · {moderatorActivity}</div>
              <p className="mt-1.5 min-h-[3rem] text-[15px] leading-relaxed">
                {room.moderatorThinking && !room.moderatorCaption ? (
                  <span className="inline-flex items-center gap-2 text-ink-400">
                    <ThinkingDots /> considering what was just said
                  </span>
                ) : (
                  room.moderatorCaption || (
                    <span className="text-ink-400">
                      {room.sessionStatus === 'CONNECTING'
                        ? 'Waiting for everyone to join…'
                        : 'Listening.'}
                    </span>
                  )
                )}
              </p>
            </div>
          </div>
        </section>

        {iWasRemoved && (
          <section className="rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <div className="label text-red-600">Removed from this discussion</div>
            <p className="mt-1 text-sm text-red-700">
              You shared personal contact details, which the ground rules do not allow.
              Your microphone has been disconnected and what you said was not recorded.
            </p>
          </section>
        )}

        {iHaveFloor && !room.ended && (
          <section className="flex flex-wrap items-center gap-3 rounded-xl border border-signal/50 bg-signal-soft px-4 py-3">
            <span className="relative flex h-2.5 w-2.5">
              <span className="absolute inline-flex h-full w-full animate-pulse-ring rounded-full bg-signal" />
              <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-signal" />
            </span>
            <span className="text-sm font-semibold text-signal-deep">
              Your turn — go ahead.
            </span>
            <span className="text-sm text-ink-300">
              Take a moment to think if you need it; you have up to{' '}
              {room.floorMaxSeconds} seconds once you start.
            </span>
          </section>
        )}

        {/* participants */}
        <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {roster.map((person) => {
            const hasFloor = room.floorHolder === person.participant_id
            const wasRemoved = room.removed.includes(person.participant_id)
            return (
              <div
                key={person.participant_id}
                className={`card relative p-4 transition ${
                  wasRemoved ? 'opacity-50' : hasFloor ? 'border-signal/70 bg-signal-soft' : ''
                }`}
              >
                <div className="relative mx-auto mb-3 flex h-14 w-14 items-center justify-center rounded-full bg-ink-700 text-sm font-bold">
                  {hasFloor && (
                    <span className="absolute inset-0 animate-pulse-ring rounded-full bg-signal/40" />
                  )}
                  {initials(person.display_name)}
                </div>
                <div className="truncate text-center text-sm font-semibold">
                  {person.display_name}
                  {person.participant_id === user?.id && (
                    <span className="text-ink-400"> (you)</span>
                  )}
                </div>
                <div className="mt-1 text-center font-mono text-[11px] text-ink-400">
                  {formatDuration(person.seconds)} · {person.turns} turns
                </div>
                <div className="mt-2 h-1 overflow-hidden rounded-full bg-ink-700">
                  <div
                    className={`h-full rounded-full transition-all duration-500 ${
                      hasFloor ? 'bg-signal' : 'bg-ink-400'
                    }`}
                    style={{ width: `${Math.round((person.seconds / maxSeconds) * 100)}%` }}
                  />
                </div>
                {wasRemoved ? (
                  <div className="mt-2 text-center font-mono text-[10px] uppercase text-red-600">
                    removed
                  </div>
                ) : (
                  !person.connected && (
                    <div className="mt-2 text-center font-mono text-[10px] uppercase text-ink-400">
                      offline
                    </div>
                  )
                )}
              </div>
            )
          })}
        </section>

        {/* controls */}
        <section className="card flex flex-wrap items-center gap-3 p-4">
          {session?.is_host ? (
            <span className="text-sm text-ink-300">
              You convened this discussion — you are listening in, not taking part.
            </span>
          ) : rtc.status === 'connected' ? (
            <>
              <button className="btn-ghost" onClick={rtc.toggleMute}>
                {rtc.muted ? 'Unmute microphone' : 'Mute microphone'}
              </button>
              <div className="flex h-2 w-28 overflow-hidden rounded-full bg-ink-700">
                <div
                  className="h-full bg-signal transition-[width] duration-75"
                  style={{ width: `${Math.round(rtc.micLevel * 100)}%` }}
                />
              </div>
              <button
                className="btn-ghost"
                disabled={!iHaveFloor}
                onClick={() => api.post(`/sessions/${sessionId}/floor/release`)}
              >
                I'm done speaking
              </button>
            </>
          ) : (
            <>
              <button
                className="btn-primary"
                onClick={rtc.connect}
                // Disabled mid-connect too, not just when the session has ended — a
                // second click here used to start a second peer connection before the
                // first one had finished, and both went on feeding the same <audio>
                // element until the first was garbage collected. That is what made a
                // speaker's voice sound doubled to everyone else.
                disabled={room.ended || rtc.status === 'requesting-mic' || rtc.status === 'negotiating'}
              >
                {rtc.status === 'idle'
                  ? 'Join with microphone'
                  : rtc.status === 'requesting-mic' || rtc.status === 'negotiating'
                    ? 'Joining…'
                    : 'Retry joining audio'}
              </button>
              <button
                className="btn-ghost"
                disabled={room.ended || joinedByText}
                onClick={async () => {
                  try {
                    await api.post(`/sessions/${sessionId}/join-text`)
                    setJoinedByText(true)
                  } catch (err) {
                    setError(err instanceof ApiError ? err.message : 'Could not join.')
                  }
                }}
              >
                {joinedByText ? 'Joined by text' : 'Join without a microphone'}
              </button>
            </>
          )}

          {session?.is_host ? (
            <button
              className="btn-danger ml-auto"
              disabled={room.ended}
              onClick={async () => {
                try {
                  await api.post(`/sessions/${sessionId}/end`, {})
                } catch (err) {
                  setError(err instanceof ApiError ? err.message : 'Could not end the session.')
                }
              }}
            >
              End discussion
            </button>
          ) : (
            <button
              className="btn-ghost ml-auto"
              onClick={async () => {
                // `rtc.disconnect()` alone only tears down a peer connection that
                // exists — for anyone who joined by text, or who never got past this
                // screen at all, it does nothing, and the server is never told they
                // left. Without this call the ledger keeps them "eligible" forever:
                // nothing else ever runs to mark them absent, so the moderator keeps
                // trying to hand them the floor for a seat that is already empty.
                try {
                  await api.del(`/sessions/${sessionId}/rtc`)
                } catch {
                  // Best-effort — leaving must never get stuck on a failed request.
                }
                rtc.disconnect()
                navigate('/')
              }}
            >
              Leave
            </button>
          )}
        </section>

        {textAllowed && !room.ended && !session?.is_host && !iWasRemoved && (
          <form onSubmit={sendText} className="card flex gap-2 p-3">
            <input
              className="input"
              placeholder={
                iHaveFloor
                  ? 'You have the floor — type your point instead of speaking'
                  : 'Type a turn (development mode)'
              }
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button className="btn-ghost whitespace-nowrap" disabled={!text.trim()}>
              Send turn
            </button>
          </form>
        )}

        {room.ended && (
          <div className="card flex flex-wrap items-center gap-4 border-signal/50 bg-signal/5 p-4">
            <div className="flex-1">
              <div className="label text-signal">Discussion finished</div>
              <p className="mt-1 text-sm">
                {room.endReason?.toLowerCase().replace(/_/g, ' ') ?? 'completed'} — the
                summary has been generated.
              </p>
            </div>
            <button className="btn-primary" onClick={() => navigate(`/recap/${sessionId}`)}>
              Read the summary
            </button>
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------ transcript */}
      <aside className="card flex max-h-[75vh] flex-col p-0">
        <div className="border-b border-ink-700 px-4 py-3">
          <div className="label">Live transcript</div>
        </div>
        <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
          {room.transcript.length === 0 && (
            <p className="text-sm text-ink-400">Nothing has been said yet.</p>
          )}
          {room.transcript.map((line) => (
            <div key={line.turnIndex} className="animate-rise">
              <div
                className={`label ${line.speaker === 'moderator' ? 'text-signal' : 'text-ink-300'}`}
              >
                {line.displayName} · {line.kind.toLowerCase()}
              </div>
              <p className="mt-1 text-[13px] leading-relaxed text-ink-200">{line.text}</p>
            </div>
          ))}
          {room.partial && (
            <div className="opacity-60">
              <div className="label">speaking…</div>
              <p className="mt-1 text-[13px] italic leading-relaxed">{room.partial.text}</p>
            </div>
          )}
          <div ref={transcriptEnd} />
        </div>
        {room.banner && (
          <div className="border-t border-ink-700 px-4 py-2 font-mono text-[11px] text-ink-400">
            {room.banner}
          </div>
        )}
      </aside>
    </div>
  )
}
