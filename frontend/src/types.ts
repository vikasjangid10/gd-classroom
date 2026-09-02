export type Role = 'SUPER_USER' | 'PARTICIPANT'

/** Self-declared, optional, and used only to pick a discussion name that fits. */
export type Gender = 'MALE' | 'FEMALE' | 'UNSPECIFIED'

export type ClassroomStatus =
  | 'DRAFT'
  | 'INVITING'
  | 'READY'
  | 'LIVE'
  | 'COMPLETED'
  | 'CANCELLED'
  | 'EXPIRED'

export type InvitationStatus = 'PENDING' | 'ACCEPTED' | 'REJECTED' | 'EXPIRED' | 'REVOKED'

export type SessionStatus =
  | 'PENDING'
  | 'CONNECTING'
  | 'ACTIVE'
  | 'SUMMARIZING'
  | 'ENDED'
  | 'ABORTED'

export interface User {
  id: string
  email: string
  display_name: string
  role: Role
  is_active: boolean
  created_at: string
}

/** Someone registered on this app, eligible to be invited into a classroom. */
export interface Participant {
  id: string
  display_name: string
  email: string
}

export interface Topic {
  id: string
  slug: string
  title: string
  description: string
  guiding_points: string[]
  difficulty: number
}

export interface Classroom {
  id: string
  title: string
  status: ClassroomStatus
  seat_count: number
  persist_transcript: boolean
  created_at: string
  expires_at: string | null
  topic: Topic
}

export interface RosterEntry {
  user_id: string
  display_name: string
  email: string
  seat_no: number | null
  invitation_id: string | null
  invitation_status: InvitationStatus | null
  responded_at: string | null
  /** Null with no error means "queued"; set means the mail server accepted it. */
  email_sent_at: string | null
  email_error: string | null
}

export interface ClassroomDetail extends Classroom {
  accepted_count: number
  pending_count: number
  roster: RosterEntry[]
  session_id: string | null
  /** Acceptances needed before a discussion can run at all — not the seat count. */
  min_to_start: number
  can_start: boolean
}

export interface Invitation {
  id: string
  classroom_id: string
  status: InvitationStatus
  expires_at: string
  responded_at: string | null
  classroom_title: string | null
  topic_title: string | null
}

/** What an emailed invitation link shows before the recipient has decided. */
export interface TokenInvitation {
  classroom_title: string
  topic_title: string
  topic_description: string
  guiding_points: string[]
  host_name: string
  invited_email: string
  invitee_name: string
  expires_at: string
  status: InvitationStatus
  seat_count: number
  accepted_count: number
  session_id: string | null
}

export interface SpeakingTally {
  participant_id: string
  display_name: string
  seconds: number
  turns: number
  connected: boolean
}

export interface LiveSnapshot {
  status: SessionStatus
  moderator_state: string
  floor_holder: string | null
  turn_index: number
  elapsed_seconds: number
  connected: string[]
  speaking_time: SpeakingTally[]
}

export interface SessionParticipant {
  user_id: string
  seat_no: number
  spoken_ms: number
  turns_taken: number
  connected_at: string | null
  display_name: string
}

export interface SessionInfo {
  id: string
  classroom_id: string
  status: SessionStatus
  end_reason: string | null
  started_at: string | null
  ended_at: string | null
  participants: SessionParticipant[]
  live: LiveSnapshot | null
  /** True when you convened this classroom — only the host may end the discussion. */
  is_host: boolean
}

export interface Tickets {
  sse_ticket: string
  rtc_ticket: string
  expires_in: number
  ice_servers: RTCIceServer[]
}

export interface Turn {
  turn_index: number
  speaker_type: 'MODERATOR' | 'PARTICIPANT'
  speaker_user_id: string | null
  speaker_name: string | null
  kind: string
  text: string
  started_at: string
  duration_ms: number
}

export interface Summary {
  status: 'PENDING' | 'READY' | 'FAILED'
  headline: string | null
  key_points: string[]
  per_participant: { name: string; contribution: string; strength: string }[]
  open_questions: string[]
  model: string | null
  error: string | null
}

/** The SSE envelope, identical for every event type. */
export interface ServerEvent<T = Record<string, unknown>> {
  v: number
  seq: number
  ts: string
  type: string
  topic: string
  payload: T
}
