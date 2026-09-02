import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '@/lib/api'
import { EmptyState, ErrorNote, Spinner } from '@/components/ui'
import type { Invitation } from '@/types'

interface RespondResult {
  classroom_id: string
  classroom_status: string
  session_id: string | null
}

export default function InvitationsPage({ revision }: { revision: number }) {
  const navigate = useNavigate()
  const [invitations, setInvitations] = useState<Invitation[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [readySession, setReadySession] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setInvitations(await api.get<Invitation[]>('/invitations'))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load your invitations.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load, revision])

  const respond = async (invitation: Invitation, accept: boolean) => {
    setBusyId(invitation.id)
    setError(null)
    try {
      const result = await api.post<RespondResult>(
        `/invitations/${invitation.id}/${accept ? 'accept' : 'reject'}`,
        accept ? undefined : { reason: null },
      )
      if (accept && result.session_id) setReadySession(result.session_id)
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not send your answer.')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Your invitations</h1>
        <p className="mt-1 text-sm text-ink-300">
          A discussion starts as soon as all four invited participants have accepted.
        </p>
      </div>

      {readySession && (
        <div className="card flex flex-wrap items-center gap-4 border-signal/50 bg-signal/5 p-4">
          <div className="flex-1">
            <div className="label text-signal">All four accepted</div>
            <p className="mt-1 text-sm">The room is open and the moderator is waiting.</p>
          </div>
          <button className="btn-primary" onClick={() => navigate(`/room/${readySession}`)}>
            Join the discussion
          </button>
        </div>
      )}

      <ErrorNote message={error} />

      {loading ? (
        <Spinner label="Loading…" />
      ) : invitations.length === 0 ? (
        <EmptyState
          title="Nothing waiting for you"
          body="When a host creates a classroom on a topic you know, an invitation will appear here in real time."
        />
      ) : (
        <div className="grid gap-3">
          {invitations.map((invitation) => (
            <div key={invitation.id} className="card animate-rise p-5">
              <div className="label">{invitation.topic_title}</div>
              <h3 className="mt-1.5 text-lg font-bold">{invitation.classroom_title}</h3>
              <p className="mt-1 font-mono text-[11px] text-ink-400">
                Expires {new Date(invitation.expires_at).toLocaleTimeString()}
              </p>
              <div className="mt-4 flex gap-2">
                <button
                  className="btn-primary"
                  disabled={busyId === invitation.id}
                  onClick={() => respond(invitation, true)}
                >
                  Accept
                </button>
                <button
                  className="btn-ghost"
                  disabled={busyId === invitation.id}
                  onClick={() => respond(invitation, false)}
                >
                  Decline
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
