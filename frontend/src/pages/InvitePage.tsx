/**
 * The page an invitation email opens.
 *
 * It is the only route that renders for a signed-out visitor, and for most participants
 * it is the first thing they ever see of this product. So it explains what they are
 * being asked to join before it asks for anything — including microphone access, which
 * is requested by the room, not here.
 *
 * Accepting signs them in: the backend returns a real access token, because possession
 * of the emailed token already proved they control the invited mailbox.
 */

import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ApiError, api, auth as tokenStore } from '@/lib/api'
import { ErrorNote, Spinner } from '@/components/ui'
import { useAuth } from '@/store/auth'
import type { TokenInvitation, User } from '@/types'

interface AcceptResponse {
  classroom_id: string
  classroom_status: string
  session_id: string | null
  access_token: string
  user: User
}

export default function InvitePage() {
  const { token = '' } = useParams()
  const navigate = useNavigate()
  const { adopt } = useAuth()

  const [invite, setInvite] = useState<TokenInvitation | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'accept' | 'reject' | null>(null)
  const [declined, setDeclined] = useState(false)

  const load = useCallback(async () => {
    try {
      const data = await api.get<TokenInvitation>(`/invitations/by-token/${token}`)
      setInvite(data)
      setName((current) => current || data.invitee_name)
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : 'This invitation link could not be opened.',
      )
    } finally {
      setLoading(false)
    }
  }, [token])

  useEffect(() => {
    void load()
  }, [load])

  const accept = async () => {
    setBusy('accept')
    setError(null)
    try {
      const result = await api.post<AcceptResponse>(`/invitations/by-token/${token}/accept`, {
        display_name: name.trim() || null,
      })
      tokenStore.set(result.access_token)
      adopt(result.user)
      // Four acceptances means the room already exists — go straight there rather than
      // dropping someone who just said yes onto a lobby they have no reason to read.
      navigate(result.session_id ? `/room/${result.session_id}` : '/invitations', {
        replace: true,
      })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not accept this invitation.')
      setBusy(null)
    }
  }

  const reject = async () => {
    setBusy('reject')
    setError(null)
    try {
      await api.post(`/invitations/by-token/${token}/reject`, { reason: null })
      setDeclined(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not decline this invitation.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-xl flex-col justify-center px-5 py-12">
      <div className="label mb-4 text-signal">AI GD Classroom</div>

      {loading ? (
        <Spinner label="Opening your invitation…" />
      ) : declined ? (
        <Notice
          title="You have declined"
          body="Thanks for letting us know — we have told the host, and you will not be called on."
        />
      ) : !invite ? (
        <Notice
          title="This link is not valid"
          body={
            error ??
            'The invitation may have expired, been withdrawn, or already been used. Ask your host to send a new one.'
          }
        />
      ) : invite.status !== 'PENDING' ? (
        <Notice
          title={
            invite.status === 'ACCEPTED'
              ? 'You have already accepted'
              : 'This invitation is closed'
          }
          body={
            invite.status === 'ACCEPTED'
              ? 'Watch your inbox — you will get the room link as soon as all four participants have accepted.'
              : `The invitation to "${invite.topic_title}" is no longer open. Ask ${invite.host_name} to send a new one.`
          }
        />
      ) : (
        <>
          <h1 className="text-2xl font-bold leading-snug tracking-tight">
            {invite.host_name} invited you to a group discussion on{' '}
            <span className="text-signal">{invite.topic_title}</span>
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-ink-300">
            {invite.topic_description}
          </p>

          {invite.guiding_points.length > 0 && (
            <div className="card mt-5 p-4">
              <div className="label mb-2">What the moderator will ask about</div>
              <ul className="grid gap-1.5 text-sm text-ink-200">
                {invite.guiding_points.map((point) => (
                  <li key={point} className="flex gap-2">
                    <span className="text-signal">·</span>
                    {point}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <dl className="mt-5 grid gap-3 sm:grid-cols-3">
            <Fact label="Participants">
              {invite.accepted_count} of {invite.seat_count} accepted
            </Fact>
            <Fact label="Format">Voice only, ~25 min</Fact>
            <Fact label="Invited as">{invite.invited_email}</Fact>
          </dl>

          <label className="mt-6 block">
            <span className="label">Your name, as the moderator will say it</span>
            <input
              className="input mt-1.5 w-full"
              value={name}
              maxLength={80}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Priya Sharma"
            />
          </label>

          <div className="mt-3">
            <ErrorNote message={error} />
          </div>

          <div className="mt-5 flex flex-wrap gap-3">
            <button className="btn-primary" onClick={accept} disabled={busy !== null}>
              {busy === 'accept' ? 'Joining…' : 'Accept and join'}
            </button>
            <button className="btn-ghost" onClick={reject} disabled={busy !== null}>
              {busy === 'reject' ? 'Declining…' : 'Decline'}
            </button>
          </div>

          <p className="mt-5 text-xs leading-relaxed text-ink-400">
            You will need a microphone and a quiet place. The discussion starts once all{' '}
            {invite.seat_count} invitees accept — we will email you the room link. Nothing you
            say is stored after the session ends beyond the transcript your host chose to keep.
          </p>
        </>
      )}
    </div>
  )
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="card px-3.5 py-3">
      <dt className="label">{label}</dt>
      <dd className="mt-1 truncate text-sm font-semibold" title={String(children)}>
        {children}
      </dd>
    </div>
  )
}

function Notice({ title, body }: { title: string; body: string }) {
  return (
    <div className="card px-6 py-10 text-center">
      <h1 className="text-xl font-bold">{title}</h1>
      <p className="mx-auto mt-2 max-w-sm text-sm leading-relaxed text-ink-300">{body}</p>
    </div>
  )
}
