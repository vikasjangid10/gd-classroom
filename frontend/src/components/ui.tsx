import type { ReactNode } from 'react'
import type { ClassroomStatus, InvitationStatus, SessionStatus } from '@/types'

const TONES = {
  neutral: 'border-ink-600 bg-ink-700/60 text-ink-300',
  live: 'border-signal/40 bg-signal-soft text-signal-deep',
  good: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  warn: 'border-amber-200 bg-amber-50 text-amber-700',
  bad: 'border-red-200 bg-red-50 text-red-600',
} as const

type Tone = keyof typeof TONES

const CLASSROOM_TONE: Record<ClassroomStatus, Tone> = {
  DRAFT: 'neutral',
  INVITING: 'warn',
  READY: 'good',
  LIVE: 'live',
  COMPLETED: 'neutral',
  CANCELLED: 'bad',
  EXPIRED: 'bad',
}

const INVITE_TONE: Record<InvitationStatus, Tone> = {
  PENDING: 'warn',
  ACCEPTED: 'good',
  REJECTED: 'bad',
  EXPIRED: 'neutral',
  REVOKED: 'neutral',
}

const SESSION_TONE: Record<SessionStatus, Tone> = {
  PENDING: 'neutral',
  CONNECTING: 'warn',
  ACTIVE: 'live',
  SUMMARIZING: 'warn',
  ENDED: 'neutral',
  ABORTED: 'bad',
}

export function Pill({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`pill ${TONES[tone]}`}>{children}</span>
}

export function ClassroomBadge({ status }: { status: ClassroomStatus }) {
  return <Pill tone={CLASSROOM_TONE[status]}>{status.toLowerCase()}</Pill>
}

export function InvitationBadge({ status }: { status: InvitationStatus }) {
  return <Pill tone={INVITE_TONE[status]}>{status.toLowerCase()}</Pill>
}

export function SessionBadge({ status }: { status: SessionStatus }) {
  return <Pill tone={SESSION_TONE[status]}>{status.toLowerCase()}</Pill>
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-ink-300">
      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-ink-500 border-t-signal" />
      {label}
    </div>
  )
}

export function EmptyState({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-14 text-center">
      <h3 className="text-lg font-bold">{title}</h3>
      <p className="max-w-md text-sm text-ink-300">{body}</p>
      {action}
    </div>
  )
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
      {message}
    </div>
  )
}

export function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}
