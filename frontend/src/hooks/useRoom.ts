import { useCallback, useMemo, useReducer } from 'react'
import type { ServerEvent, SessionStatus, SpeakingTally } from '@/types'

export interface TranscriptLine {
  turnIndex: number
  speaker: 'moderator' | 'participant'
  participantId: string | null
  displayName: string
  text: string
  kind: string
}

export interface RoomState {
  sessionStatus: SessionStatus
  moderatorState: string
  moderatorCaption: string
  /** The caption holds a completed utterance; the next sentence starts a new one. */
  moderatorCaptionClosed: boolean
  moderatorSpeaking: boolean
  /** The host is weighing what was just said — shown so the pause reads as attention. */
  moderatorThinking: boolean
  floorHolder: string | null
  floorMaxSeconds: number
  /** How long the holder has to *begin* — distinct from the cap on the turn itself. */
  floorSecondsToBegin: number
  floorStartedAt: number | null
  partial: { participantId: string; text: string } | null
  transcript: TranscriptLine[]
  speakingTime: SpeakingTally[]
  connected: string[]
  /** Taken out of the round by the moderator — for this discussion, permanently. */
  removed: string[]
  turnIndex: number
  summaryReady: boolean
  ended: boolean
  endReason: string | null
  banner: string | null
}

const initial: RoomState = {
  sessionStatus: 'PENDING',
  moderatorState: 'IDLE',
  moderatorCaption: '',
  moderatorCaptionClosed: true,
  moderatorSpeaking: false,
  moderatorThinking: false,
  floorHolder: null,
  floorMaxSeconds: 90,
  floorSecondsToBegin: 25,
  floorStartedAt: null,
  partial: null,
  transcript: [],
  speakingTime: [],
  connected: [],
  removed: [],
  turnIndex: 0,
  summaryReady: false,
  ended: false,
  endReason: null,
  banner: null,
}

type Action = { type: 'event'; event: ServerEvent } | { type: 'reset' } | { type: 'hydrate'; state: Partial<RoomState> }

/**
 * Every reducer branch is idempotent and takes absolute values from the payload, never
 * deltas. That is what makes replaying missed events after a reconnect safe.
 */
function reduce(state: RoomState, action: Action): RoomState {
  if (action.type === 'reset') return initial
  if (action.type === 'hydrate') return { ...state, ...action.state }

  const { type, payload } = action.event
  const p = payload as Record<string, never> as Record<string, any>

  switch (type) {
    case 'session.state':
      return { ...state, sessionStatus: p.to as SessionStatus }

    case 'participant.connected':
      return {
        ...state,
        connected: state.connected.includes(p.participant_id)
          ? state.connected
          : [...state.connected, p.participant_id],
        banner: `${p.display_name} joined`,
      }

    case 'participant.disconnected':
      return {
        ...state,
        connected: state.connected.filter((id) => id !== p.participant_id),
        banner: `${p.display_name} dropped out`,
      }

    case 'participant.removed':
      return {
        ...state,
        connected: state.connected.filter((id) => id !== p.participant_id),
        removed: state.removed.includes(p.participant_id)
          ? state.removed
          : [...state.removed, p.participant_id],
        floorHolder: state.floorHolder === p.participant_id ? null : state.floorHolder,
        banner: `${p.display_name} was removed for sharing personal contact details`,
      }

    case 'moderator.speaking': {
      // Sentences of one utterance accumulate; the final frame carries the whole thing.
      // The flag is what separates two utterances: without it the ground rules were
      // appended to the introduction and the caption grew all session.
      const startsNew = state.moderatorCaptionClosed
      return {
        ...state,
        moderatorCaption: p.is_final
          ? p.text
          : startsNew
            ? p.text
            : `${state.moderatorCaption} ${p.text}`.trim(),
        moderatorCaptionClosed: Boolean(p.is_final),
        moderatorSpeaking: !p.is_final,
        moderatorThinking: false,
      }
    }

    case 'moderator.thinking':
      return {
        ...state,
        moderatorThinking: true,
        moderatorCaption: '',
        moderatorCaptionClosed: true,
      }

    case 'moderator.interrupted':
      return { ...state, moderatorSpeaking: false, banner: 'Moderator yielded' }

    case 'floor.granted':
      return {
        ...state,
        floorHolder: p.participant_id,
        floorMaxSeconds: p.max_seconds ?? 90,
        floorSecondsToBegin: p.seconds_to_begin ?? 25,
        floorStartedAt: Date.now(),
        turnIndex: p.turn_index ?? state.turnIndex,
        moderatorSpeaking: false,
        moderatorThinking: false,
        partial: null,
        banner: `${p.display_name} has the floor`,
      }

    case 'floor.released':
      return { ...state, floorHolder: null, floorStartedAt: null, partial: null }

    case 'transcript.partial':
      return { ...state, partial: { participantId: p.participant_id, text: p.text } }

    case 'transcript.final': {
      if (state.transcript.some((line) => line.turnIndex === p.turn_index)) return state
      const line: TranscriptLine = {
        turnIndex: p.turn_index,
        speaker: p.speaker,
        participantId: p.participant_id ?? null,
        displayName: p.display_name,
        text: p.text,
        kind: p.kind,
      }
      return {
        ...state,
        partial: null,
        transcript: [...state.transcript, line].sort((a, b) => a.turnIndex - b.turnIndex),
      }
    }

    case 'speaking_time.updated':
      return { ...state, speakingTime: p.participants ?? [] }

    case 'session.summary_ready':
      return { ...state, summaryReady: p.status === 'READY' }

    case 'session.ended':
      return {
        ...state,
        ended: true,
        endReason: p.reason ?? null,
        floorHolder: null,
        moderatorSpeaking: false,
      }

    case 'error':
      return { ...state, banner: String(p.message ?? 'Something went wrong') }

    default:
      return state
  }
}

export function useRoom() {
  const [state, dispatch] = useReducer(reduce, initial)

  const onEvent = useCallback((event: ServerEvent) => {
    if (event.type === 'stream.resync') {
      dispatch({ type: 'reset' })
      return
    }
    dispatch({ type: 'event', event })
  }, [])

  const hydrate = useCallback((partial: Partial<RoomState>) => {
    dispatch({ type: 'hydrate', state: partial })
  }, [])

  const totalSeconds = useMemo(
    () => state.speakingTime.reduce((sum, tally) => sum + tally.seconds, 0),
    [state.speakingTime],
  )

  return { room: state, onEvent, hydrate, totalSeconds }
}
