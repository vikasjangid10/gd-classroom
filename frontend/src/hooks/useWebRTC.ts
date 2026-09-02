import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'
import type { Tickets } from '@/types'

export type RtcStatus = 'idle' | 'requesting-mic' | 'negotiating' | 'connected' | 'failed'

interface Options {
  sessionId: string
  /** Fetch a fresh RTC ticket plus the ICE servers to use. */
  getTickets: () => Promise<Tickets>
}

/**
 * One peer connection to the server, carrying the microphone up and the mixed
 * moderator/floor audio down.
 *
 * Signalling is non-trickle: we wait for ICE gathering to finish and post one complete
 * offer. It costs a few hundred milliseconds at join time and removes an entire
 * signalling channel from the system.
 */
export function useWebRTC({ sessionId, getTickets }: Options) {
  const [status, setStatus] = useState<RtcStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  const [micLevel, setMicLevel] = useState(0)
  const [muted, setMuted] = useState(false)

  const pcRef = useRef<RTCPeerConnection | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const rafRef = useRef<number>()

  const teardown = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    pcRef.current?.close()
    pcRef.current = null
    if (audioRef.current) {
      audioRef.current.srcObject = null
    }
    setStatus('idle')
    setMicLevel(0)
  }, [])

  const connect = useCallback(async () => {
    // A rejoin must not leave the old peer connection running alongside the new one —
    // both would still be receiving the mixed audio track, and whichever last touched
    // `audioRef.current.srcObject` would not be the only one actually running, so a
    // moment of overlap (or a client left holding a live but orphaned connection) reads
    // to a listener as the room's audio doubling or stuttering.
    teardown()
    setError(null)
    try {
      setStatus('requesting-mic')
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
        video: false,
      })
      streamRef.current = stream
      startLevelMeter(stream)

      setStatus('negotiating')
      const tickets = await getTickets()

      const pc = new RTCPeerConnection({ iceServers: tickets.ice_servers })
      pcRef.current = pc

      stream.getAudioTracks().forEach((track) => pc.addTrack(track, stream))

      pc.ontrack = (event) => {
        if (audioRef.current) {
          audioRef.current.srcObject = event.streams[0] ?? new MediaStream([event.track])
          void audioRef.current.play().catch(() => {
            setError('Click anywhere to allow audio playback.')
          })
        }
      }

      pc.onconnectionstatechange = () => {
        if (pc.connectionState === 'connected') setStatus('connected')
        if (pc.connectionState === 'failed') {
          setStatus('failed')
          setError('The audio connection failed. Check your network and rejoin.')
        }
      }

      const offer = await pc.createOffer({ offerToReceiveAudio: true })
      await pc.setLocalDescription(offer)
      await waitForIceGathering(pc)

      const answer = await api.post<{ sdp: string; type: RTCSdpType }>(
        `/sessions/${sessionId}/rtc/offer`,
        { sdp: pc.localDescription!.sdp, type: pc.localDescription!.type, ticket: tickets.rtc_ticket },
      )
      await pc.setRemoteDescription(new RTCSessionDescription(answer))
    } catch (err) {
      setStatus('failed')
      setError(err instanceof Error ? err.message : 'Could not join the audio room.')
      teardown()
    }
  }, [sessionId, getTickets, teardown])

  const toggleMute = useCallback(() => {
    const tracks = streamRef.current?.getAudioTracks() ?? []
    const next = !muted
    tracks.forEach((track) => (track.enabled = !next))
    setMuted(next)
  }, [muted])

  const startLevelMeter = (stream: MediaStream) => {
    const context = new AudioContext()
    const analyser = context.createAnalyser()
    analyser.fftSize = 512
    context.createMediaStreamSource(stream).connect(analyser)
    const buffer = new Uint8Array(analyser.frequencyBinCount)

    const tick = () => {
      analyser.getByteTimeDomainData(buffer)
      let sum = 0
      for (const sample of buffer) {
        const centred = sample - 128
        sum += centred * centred
      }
      setMicLevel(Math.min(1, Math.sqrt(sum / buffer.length) / 40))
      rafRef.current = requestAnimationFrame(tick)
    }
    tick()
  }

  useEffect(() => teardown, [teardown])

  return { status, error, micLevel, muted, connect, disconnect: teardown, toggleMute, audioRef }
}

function waitForIceGathering(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve) => {
    // Cap the wait: a single unreachable STUN server should not delay joining.
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check)
      clearTimeout(timeout)
      resolve()
    }
    const check = () => {
      if (pc.iceGatheringState === 'complete') done()
    }
    const timeout = setTimeout(done, 2500)
    pc.addEventListener('icegatheringstatechange', check)
  })
}
