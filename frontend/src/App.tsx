import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api } from '@/lib/api'
import { Layout } from '@/components/Layout'
import { Spinner } from '@/components/ui'
import { StreamProvider, useStreamEvents } from '@/store/stream'
import { useAuth } from '@/store/auth'
import ClassroomPage from '@/pages/ClassroomPage'
import DashboardPage from '@/pages/DashboardPage'
import InvitationsPage from '@/pages/InvitationsPage'
import InvitePage from '@/pages/InvitePage'
import LlmPage from '@/pages/LlmPage'
import LoginPage from '@/pages/LoginPage'
import RecapPage from '@/pages/RecapPage'
import RoomPage from '@/pages/RoomPage'
import type { ServerEvent } from '@/types'

interface InvitedNotice {
  topic: string
  classroom: string
}

export default function App() {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Restoring your session…" />
      </div>
    )
  }

  // /invite/:token is public on purpose: the recipient has no account yet, and the
  // token in the URL is the credential. It is listed in both trees so that following a
  // link while already signed in as someone else still opens the invitation.
  if (!user) {
    return (
      <Routes>
        <Route path="/invite/:token" element={<InvitePage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  return (
    <StreamProvider enabled>
      <SignedIn />
    </StreamProvider>
  )
}

function SignedIn() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [revision, setRevision] = useState(0)
  const [pending, setPending] = useState(0)
  const [readySession, setReadySession] = useState<string | null>(null)
  const [readyCount, setReadyCount] = useState(0)
  const [invited, setInvited] = useState<InvitedNotice | null>(null)

  // The tab's single stream carries lobby events wherever you are, so both the "you
  // have been invited" and "your discussion is ready" prompts work while you are
  // looking at something else. This is the whole notification system: no email, no
  // polling, no service worker — one stream the tab already has open.
  const onEvent = useCallback((event: ServerEvent) => {
    switch (event.type) {
      case 'invitation.sent': {
        const payload = event.payload as {
          topic_title?: string
          classroom_title?: string
        }
        setInvited({
          topic: payload.topic_title ?? 'a discussion',
          classroom: payload.classroom_title ?? '',
        })
        setPending((count) => count + 1)
        setRevision((r) => r + 1)
        break
      }
      case 'invitation.responded':
      case 'classroom.updated':
        setRevision((r) => r + 1)
        break
      case 'session.ready': {
        const payload = event.payload as { session_id: string; participants?: number }
        setReadySession(String(payload.session_id))
        setReadyCount(payload.participants ?? 0)
        setInvited(null)
        setRevision((r) => r + 1)
        break
      }
      case 'stream.resync':
        setRevision((r) => r + 1)
        break
    }
  }, [])

  const streamStatus = useStreamEvents(onEvent)

  useEffect(() => {
    if (user?.role === 'PARTICIPANT') {
      api
        .get<unknown[]>('/invitations')
        .then((list) => setPending(list.length))
        .catch(() => undefined)
    }
  }, [user, revision])

  const home = user?.role === 'SUPER_USER' ? '/classrooms' : '/invitations'

  return (
    <Layout streamStatus={streamStatus} pendingCount={pending}>
      {invited && !readySession && (
        <button
          data-testid="invite-toast"
          onClick={() => {
            navigate('/invitations')
            setInvited(null)
          }}
          className="mb-5 flex w-full animate-rise items-center gap-3 rounded-xl border border-amber-500/50 bg-amber-500/10 px-4 py-3 text-left transition hover:bg-amber-500/15"
        >
          <span className="label text-amber-300">Invitation</span>
          <span className="min-w-0 text-sm font-semibold">
            You have been invited to a group discussion on {invited.topic}
          </span>
          <span className="ml-auto shrink-0 text-amber-300">Respond →</span>
        </button>
      )}

      {readySession && (
        <button
          onClick={() => {
            navigate(`/room/${readySession}`)
            setReadySession(null)
          }}
          className="mb-5 flex w-full items-center gap-3 rounded-xl border border-signal/50 bg-signal/10 px-4 py-3 text-left transition hover:bg-signal/15"
        >
          <span className="label text-signal">Ready</span>
          <span className="text-sm font-semibold">
            {readyCount > 0
              ? `${readyCount} participants are in — join the discussion`
              : 'Your discussion is ready — join now'}
          </span>
          <span className="ml-auto text-signal">→</span>
        </button>
      )}

      <Routes>
        <Route path="/" element={<Navigate to={home} replace />} />
        <Route path="/login" element={<Navigate to={home} replace />} />
        <Route path="/invite/:token" element={<InvitePage />} />
        <Route path="/classrooms" element={<DashboardPage revision={revision} />} />
        <Route path="/classrooms/:classroomId" element={<ClassroomPage revision={revision} />} />
        <Route path="/invitations" element={<InvitationsPage revision={revision} />} />
        <Route path="/llm" element={<LlmPage />} />
        <Route path="/room/:sessionId" element={<RoomPage />} />
        <Route path="/recap/:sessionId" element={<RecapPage />} />
        <Route path="*" element={<Navigate to={home} replace />} />
      </Routes>
    </Layout>
  )
}
