import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, api } from '@/lib/api'
import { ErrorNote, Spinner, formatDuration } from '@/components/ui'
import type { SessionInfo, Summary, Turn } from '@/types'

export default function RecapPage() {
  const { sessionId = '' } = useParams()
  const [summary, setSummary] = useState<Summary | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const [summaryResult, turnResult, infoResult] = await Promise.allSettled([
        api.get<Summary>(`/sessions/${sessionId}/summary`),
        api.get<Turn[]>(`/sessions/${sessionId}/transcript`),
        api.get<SessionInfo>(`/sessions/${sessionId}`),
      ])
      if (cancelled) return
      if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value)
      else if (summaryResult.reason instanceof ApiError) setError(summaryResult.reason.message)
      if (turnResult.status === 'fulfilled') setTurns(turnResult.value)
      if (infoResult.status === 'fulfilled') setSession(infoResult.value)
      setLoading(false)
    })()
    return () => {
      cancelled = true
    }
  }, [sessionId])

  if (loading) return <Spinner label="Loading the recap…" />

  const duration =
    session?.started_at && session?.ended_at
      ? Math.round(
          (new Date(session.ended_at).getTime() - new Date(session.started_at).getTime()) / 1000,
        )
      : 0

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="label hover:text-ink-100">
          ← Back
        </Link>
        <h1 className="mt-2 text-2xl font-bold tracking-tight">
          {summary?.headline ?? 'Discussion recap'}
        </h1>
        <p className="mt-1 font-mono text-[11px] text-ink-400">
          {turns.length} turns · {formatDuration(duration)} ·{' '}
          {session?.end_reason?.toLowerCase().replace(/_/g, ' ')}
        </p>
      </div>

      <ErrorNote message={error} />

      {summary?.status === 'READY' && (
        <>
          <section className="card p-5">
            <div className="label mb-3">Key points</div>
            <ul className="space-y-2">
              {summary.key_points.map((point, index) => (
                <li key={index} className="flex gap-3 text-sm leading-relaxed">
                  <span className="font-mono text-[11px] text-signal">
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  {point}
                </li>
              ))}
            </ul>
          </section>

          <section className="grid gap-3 sm:grid-cols-2">
            {summary.per_participant.map((entry) => (
              <div key={entry.name} className="card p-4">
                <div className="text-sm font-bold">{entry.name}</div>
                <p className="mt-1.5 text-[13px] leading-relaxed text-ink-300">
                  {entry.contribution}
                </p>
                <div className="pill mt-3 border-signal/40 text-signal">{entry.strength}</div>
              </div>
            ))}
          </section>

          {summary.open_questions.length > 0 && (
            <section className="card p-5">
              <div className="label mb-3">Left open</div>
              <ul className="space-y-1.5 text-sm text-ink-200">
                {summary.open_questions.map((question, index) => (
                  <li key={index}>· {question}</li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}

      {summary?.status === 'FAILED' && (
        <div className="card p-5 text-sm text-ink-300">
          The summary could not be generated ({summary.error}). The full transcript below
          was still saved.
        </div>
      )}

      <section className="card p-0">
        <div className="border-b border-ink-700 px-5 py-3">
          <div className="label">Full transcript</div>
        </div>
        <div className="divide-y divide-ink-700">
          {turns.map((turn) => (
            <div key={turn.turn_index} className="px-5 py-3">
              <div
                className={`label ${
                  turn.speaker_type === 'MODERATOR' ? 'text-signal' : 'text-ink-300'
                }`}
              >
                {turn.speaker_name ?? (turn.speaker_type === 'MODERATOR' ? 'Moderator' : 'Participant')}{' '}
                · {turn.kind.toLowerCase()} · {formatDuration(turn.duration_ms / 1000)}
              </div>
              <p className="mt-1 text-sm leading-relaxed text-ink-200">{turn.text}</p>
            </div>
          ))}
          {turns.length === 0 && (
            <p className="px-5 py-6 text-sm text-ink-400">
              No transcript was kept for this discussion.
            </p>
          )}
        </div>
      </section>
    </div>
  )
}
