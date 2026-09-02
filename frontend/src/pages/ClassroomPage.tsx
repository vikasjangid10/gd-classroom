import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ApiError, api } from '@/lib/api'
import { ClassroomBadge, ErrorNote, InvitationBadge, Spinner } from '@/components/ui'
import type { ClassroomDetail, Participant, RosterEntry } from '@/types'

/**
 * Only rendered when email is switched on. With in-app invitations there is nothing to
 * report: the invitation was published to the invitee's own event stream inside the same
 * transaction that created it, so "delivered" and "exists" are the same fact.
 */
function DeliveryNote({ entry }: { entry: RosterEntry }) {
  if (entry.email_error) {
    return (
      <div className="mt-0.5 text-[11px] text-red-300" title={entry.email_error}>
        email failed — resend
      </div>
    )
  }
  if (entry.email_sent_at) {
    return (
      <div className="mt-0.5 text-[11px] text-ink-500">
        emailed {new Date(entry.email_sent_at).toLocaleTimeString()}
      </div>
    )
  }
  return null
}

export default function ClassroomPage({ revision }: { revision: number }) {
  const { classroomId = '' } = useParams()
  const navigate = useNavigate()
  const [detail, setDetail] = useState<ClassroomDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [replacement, setReplacement] = useState('')
  const [participants, setParticipants] = useState<Participant[]>([])

  const load = useCallback(async () => {
    try {
      const [room, people] = await Promise.all([
        api.get<ClassroomDetail>(`/classrooms/${classroomId}`),
        api.get<Participant[]>('/users/participants'),
      ])
      setDetail(room)
      setParticipants(people)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load this classroom.')
    }
  }, [classroomId])

  useEffect(() => {
    void load()
  }, [load, revision])

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'That did not work.')
    } finally {
      setBusy(false)
    }
  }

  if (!detail) {
    return error ? <ErrorNote message={error} /> : <Spinner label="Loading classroom…" />
  }

  const seats = Array.from({ length: detail.seat_count }, (_, index) => index)
  const accepted = detail.roster.filter((r) => r.invitation_status === 'ACCEPTED')
  const freeSeats = detail.seat_count - accepted.length - detail.pending_count
  // Anyone already on the roster has answered or is being waited on; offering them
  // again would only produce a conflict the host cannot act on.
  const involved = new Set(detail.roster.map((entry) => entry.email))
  const available = participants.filter((person) => !involved.has(person.email))

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-0 flex-1">
          <Link to="/classrooms" className="label hover:text-ink-100">
            ← All classrooms
          </Link>
          <h1 className="mt-2 text-2xl font-bold tracking-tight">{detail.title}</h1>
          <p className="mt-1 text-sm text-ink-300">{detail.topic.description}</p>
        </div>
        <ClassroomBadge status={detail.status} />
      </div>

      <section className="card p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <div className="label">Acceptances</div>
            <div className="mt-1 text-3xl font-bold tabular-nums">
              {detail.accepted_count}
              <span className="text-ink-400"> / {detail.seat_count}</span>
            </div>
          </div>
          <div className="flex gap-1.5">
            {seats.map((index) => (
              <span
                key={index}
                className={`h-8 w-8 rounded-lg border ${
                  index < detail.accepted_count
                    ? 'border-signal bg-signal/20'
                    : 'border-ink-600 bg-ink-900'
                }`}
              />
            ))}
          </div>
        </div>

        <div className="divide-y divide-ink-700 rounded-lg border border-ink-700">
          {detail.roster.length === 0 && (
            <p className="px-4 py-6 text-center text-sm text-ink-400">
              No invitations have been sent yet.
            </p>
          )}
          {detail.roster.map((entry) => (
            <div
              key={entry.invitation_id ?? `${entry.user_id}-${entry.invitation_status}`}
              className="flex flex-wrap items-center gap-3 px-4 py-2.5"
            >
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-ink-700 text-xs font-bold">
                {entry.display_name.slice(0, 2).toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold">{entry.display_name}</div>
                <div data-testid="roster-email" className="font-mono text-[11px] text-ink-400">
                  {entry.email}
                </div>
                <DeliveryNote entry={entry} />
              </div>
              {entry.seat_no && <span className="label">seat {entry.seat_no}</span>}
              {entry.invitation_status && <InvitationBadge status={entry.invitation_status} />}
              {entry.invitation_id &&
                (entry.invitation_status === 'PENDING' ||
                  entry.invitation_status === 'EXPIRED') && (
                  <button
                    className="label hover:text-ink-100"
                    disabled={busy}
                    onClick={() =>
                      act(() =>
                        api.post(
                          `/classrooms/${detail.id}/invitations/${entry.invitation_id}/resend`,
                        ),
                      )
                    }
                  >
                    re-invite
                  </button>
                )}
            </div>
          ))}
        </div>

        <ErrorNote message={error} />

        <div className="mt-4 flex flex-wrap items-center gap-2">
          {detail.can_start && (
            <button
              className="btn-primary"
              disabled={busy}
              onClick={() =>
                act(async () => {
                  const result = await api.post<{ session_id: string }>(
                    `/classrooms/${detail.id}/start`,
                  )
                  navigate(`/room/${result.session_id}`)
                })
              }
            >
              {detail.session_id
                ? 'Open the discussion room'
                : `Start with ${accepted.length}`}
            </button>
          )}
          {!detail.can_start && detail.status === 'INVITING' && (
            <span className="text-sm text-ink-400">
              {accepted.length} accepted — {detail.min_to_start} needed to begin.
            </span>
          )}
          {freeSeats > 0 && (detail.status === 'INVITING' || detail.status === 'READY') && (
            <form
              className="flex flex-wrap items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                if (!replacement) return
                void act(async () => {
                  await api.post(`/classrooms/${detail.id}/invitations`, {
                    emails: [replacement],
                  })
                  setReplacement('')
                })
              }}
            >
              <select
                className="input w-60"
                data-testid="replacement-picker"
                value={replacement}
                onChange={(e) => setReplacement(e.target.value)}
              >
                <option value="">
                  Fill {freeSeats} free seat{freeSeats === 1 ? '' : 's'}…
                </option>
                {available.map((person) => (
                  <option key={person.id} value={person.email}>
                    {person.display_name} · {person.email}
                  </option>
                ))}
              </select>
              <button className="btn-ghost" type="submit" disabled={busy || !replacement}>
                Invite
              </button>
            </form>
          )}
          {detail.status === 'COMPLETED' && detail.session_id && (
            <Link className="btn-ghost" to={`/recap/${detail.session_id}`}>
              View summary
            </Link>
          )}
          {!['COMPLETED', 'CANCELLED', 'EXPIRED'].includes(detail.status) && (
            <button
              className="btn-danger ml-auto"
              disabled={busy}
              onClick={() => act(() => api.post(`/classrooms/${detail.id}/cancel`, {}))}
            >
              Cancel classroom
            </button>
          )}
        </div>
      </section>
    </div>
  )
}
