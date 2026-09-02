/**
 * Both routers, on one page.
 *
 * The question this page exists to answer is not "is the LLM up" — it is "which rung is
 * answering right now, and why is it not the one above it". So every row states its own
 * reason, and the event feed is *shared* between the lanes rather than split: the two
 * chains run on the same providers and the same keys, and the interleaving is usually
 * the whole story.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '@/lib/api'
import { ErrorNote, Pill, Spinner } from '@/components/ui'

const POLL_MS = 5000

type TierStatus =
  | 'ACTIVE'
  | 'READY'
  | 'THROTTLED'
  | 'QUOTA_SPENT'
  | 'FAILING'
  | 'MISCONFIGURED'
  | 'UNAVAILABLE'

interface Tier {
  name: string
  status: TierStatus
  model: string
  is_local: boolean
  unavailable_reason: string
  quota_day: string
  requests: number
  request_limit: number | null
  tokens: number
  prompt_tokens: number
  completion_tokens: number
  token_limit: number | null
  bench_reason: string
  bench_detail: string
  seconds_until_clear: number | null
  failure_streak: number
  last_throttle_detail: string
}

interface Lane {
  lane: 'fast' | 'deep'
  role: string
  active: string
  next_up: string
  tiers: Tier[]
  requests_today: number
  prompt_tokens_today: number
  completion_tokens_today: number
}

/** Thousands, because raw token counts stop being readable after about four digits. */
function tokens(n: number): string {
  if (n < 1000) return String(n)
  return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`
}

interface ChainEvent {
  at: number
  kind: string
  tier: string
  lane: string
  purpose: string
  detail: string
  successor: string
  clears_at: number | null
  extra: Record<string, unknown>
}

interface ChainReport {
  enabled: boolean
  reason?: string
  assessment?: boolean
  lanes?: Lane[]
  recent_events?: ChainEvent[]
}

const TIER_TONE: Record<TierStatus, 'good' | 'live' | 'warn' | 'bad' | 'neutral'> = {
  ACTIVE: 'live',
  READY: 'good',
  THROTTLED: 'warn',
  QUOTA_SPENT: 'warn',
  FAILING: 'bad',
  MISCONFIGURED: 'bad',
  UNAVAILABLE: 'neutral',
}

/** Event kinds worth colouring. Anything routine stays quiet on purpose. */
const EVENT_TONE: Record<string, 'good' | 'warn' | 'bad' | 'neutral'> = {
  tier_recovered: 'good',
  tier_selected: 'neutral',
  tier_skipped: 'neutral',
  tier_throttled: 'warn',
  tier_exhausted: 'warn',
  tier_failing: 'bad',
  tier_misconfigured: 'bad',
  chain_exhausted: 'bad',
}

const LANE_LABEL: Record<string, string> = {
  fast: 'Fast',
  deep: 'Deep',
}

function countdown(seconds: number | null): string {
  if (seconds === null || seconds <= 0) return ''
  if (seconds < 90) return `${Math.round(seconds)}s`
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`
  return `${(seconds / 3600).toFixed(1)}h`
}

function clock(at: number): string {
  return new Date(at * 1000).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/**
 * The one line that says why this rung is or is not serving. Bench detail first: when a
 * tier is out, the provider's own words are the most useful thing on the page.
 */
function why(tier: Tier): string {
  if (tier.status === 'UNAVAILABLE') return tier.unavailable_reason
  if (tier.bench_detail) return tier.bench_detail
  if (tier.last_throttle_detail) return tier.last_throttle_detail
  if (tier.failure_streak) return `${tier.failure_streak} failure(s) in a row`
  return ''
}

/**
 * A quota bench clears at the provider's own rollover and nothing here can hurry it, so
 * the button is offered only for the two reasons that are guesses about the outside
 * world — a key that has since been fixed, a provider that has since come back.
 */
const CLEARABLE: TierStatus[] = ['FAILING', 'MISCONFIGURED']

function TierRow({ tier, onClear }: { tier: Tier; onClear: (tier: string) => void }) {
  const clears = countdown(tier.seconds_until_clear)
  const reason = why(tier)
  return (
    <tr className="border-t border-ink-600/60 align-top">
      <td className="py-2 pr-3">
        <div className="font-semibold">{tier.name}</div>
        <div className="text-xs text-ink-400">{tier.model}</div>
      </td>
      <td className="py-2 pr-3 whitespace-nowrap">
        <Pill tone={TIER_TONE[tier.status]}>{tier.status.toLowerCase().replace('_', ' ')}</Pill>
        {clears ? <div className="mt-1 text-xs text-ink-400">back in {clears}</div> : null}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums whitespace-nowrap">
        {tier.requests}
        {tier.request_limit ? <span className="text-ink-400"> / {tier.request_limit}</span> : null}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums whitespace-nowrap">
        {tier.tokens ? (
          <>
            {tokens(tier.prompt_tokens)}
            <span className="text-ink-400"> / </span>
            {tokens(tier.completion_tokens)}
          </>
        ) : (
          <span className="text-ink-500">—</span>
        )}
      </td>
      <td className="py-2 pl-2 text-xs text-ink-300">
        {reason || <span className="text-ink-500">—</span>}
        {CLEARABLE.includes(tier.status) ? (
          <button
            className="btn-ghost mt-1 block !px-2 !py-1 text-[11px]"
            onClick={() => onClear(tier.name)}
          >
            Put back in rotation
          </button>
        ) : null}
      </td>
    </tr>
  )
}

function LaneCard({ lane, onClear }: { lane: Lane; onClear: (tier: string) => void }) {
  return (
    <section className="card">
      <header className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-base font-bold">{LANE_LABEL[lane.lane] ?? lane.lane} router</h2>
        <span className="text-sm text-ink-300">{lane.role}</span>
        <span className="ml-auto text-xs text-ink-400 tabular-nums">
          {lane.requests_today} calls · {tokens(lane.prompt_tokens_today)} in ·{' '}
          {tokens(lane.completion_tokens_today)} out today
        </span>
      </header>

      <div className="mb-3 flex flex-wrap gap-4 text-sm">
        <div>
          <span className="text-ink-400">served last </span>
          <span className="font-semibold">{lane.active}</span>
        </div>
        <div>
          {/* Distinct from "active" on purpose — they differ for exactly as long as it
              takes somebody to notice something is wrong and open this page. */}
          <span className="text-ink-400">next up </span>
          <span className="font-semibold">{lane.next_up}</span>
        </div>
      </div>

      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-ink-400">
          <tr>
            <th className="pb-1 font-semibold">Rung</th>
            <th className="pb-1 font-semibold">Status</th>
            <th className="pb-1 pr-3 text-right font-semibold">Calls</th>
            <th className="pb-1 pr-3 text-right font-semibold">Tokens in / out</th>
            <th className="pb-1 pl-2 font-semibold">Why</th>
          </tr>
        </thead>
        <tbody>
          {lane.tiers.map((tier) => (
            <TierRow key={tier.name} tier={tier} onClear={onClear} />
          ))}
        </tbody>
      </table>
    </section>
  )
}

function EventRow({ event }: { event: ChainEvent }) {
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-t border-ink-600/60 py-1.5">
      <span className="w-20 shrink-0 text-xs tabular-nums text-ink-400">{clock(event.at)}</span>
      <Pill tone={event.lane === 'deep' ? 'warn' : 'neutral'}>
        {LANE_LABEL[event.lane] ?? event.lane}
      </Pill>
      <Pill tone={EVENT_TONE[event.kind] ?? 'neutral'}>
        {event.kind.replace(/^tier_/, '').replace(/_/g, ' ')}
      </Pill>
      <span className="font-semibold">{event.tier || '(chain)'}</span>
      {event.purpose ? <span className="text-xs text-ink-400">{event.purpose}</span> : null}
      {event.successor ? (
        <span className="text-xs text-ink-400">→ {event.successor}</span>
      ) : null}
      {event.detail ? (
        <span className="w-full text-xs text-ink-300 sm:w-auto">{event.detail}</span>
      ) : null}
    </li>
  )
}

export default function LlmPage() {
  const [report, setReport] = useState<ChainReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [paused, setPaused] = useState(false)
  // Kept in a ref so the polling effect does not restart on every tick.
  const pausedRef = useRef(paused)
  pausedRef.current = paused

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setReport(await api.get<ChainReport>('/llm/chain', signal))
      setError(null)
    } catch (err) {
      if (signal?.aborted) return
      setError(err instanceof ApiError ? err.message : 'Could not read the routers.')
    }
  }, [])

  const clearBench = useCallback(
    async (tier: string) => {
      try {
        await api.post(`/llm/tiers/${encodeURIComponent(tier)}/clear-bench`)
      } catch (err) {
        setError(err instanceof ApiError ? err.message : `Could not clear ${tier}.`)
        return
      }
      // Read it back rather than patching the row: the rung may have been re-benched
      // between the click and the reply, and a row that says READY when it is not is
      // worse than one that took an extra second to say so.
      await load()
    },
    [load],
  )

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    const timer = window.setInterval(() => {
      if (!pausedRef.current) void load()
    }, POLL_MS)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [load])

  if (!report && !error) return <Spinner label="Reading the routers…" />

  if (report && !report.enabled) {
    return (
      <div className="card">
        <h1 className="text-lg font-bold">LLM routers</h1>
        <p className="mt-2 text-sm text-ink-300">
          No chain is running — {report.reason}.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-lg font-bold">LLM routers</h1>
        <Pill tone={report?.assessment ? 'good' : 'neutral'}>
          answer assessment {report?.assessment ? 'on' : 'off'}
        </Pill>
        <button
          className="btn-ghost ml-auto !px-3 !py-1.5 text-xs"
          onClick={() => setPaused((p) => !p)}
        >
          {paused ? 'Resume' : 'Pause'} refresh
        </button>
      </header>

      <ErrorNote message={error} />

      <div className="grid gap-5 lg:grid-cols-2">
        {report?.lanes?.map((lane) => (
          <LaneCard key={lane.lane} lane={lane} onClear={clearBench} />
        ))}
      </div>

      <section className="card">
        <header className="mb-2 flex items-baseline gap-3">
          <h2 className="text-base font-bold">Recent transitions</h2>
          <span className="text-xs text-ink-400">
            both routers, newest first — only state changes are recorded, never one line
            per call
          </span>
        </header>
        {report?.recent_events?.length ? (
          <ul className="text-sm">
            {report.recent_events.map((event, index) => (
              <EventRow key={`${event.at}-${event.tier}-${index}`} event={event} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-300">
            Nothing has changed since the last restart. On this page that is the good
            outcome: every rung is answering when it is asked.
          </p>
        )}
      </section>
    </div>
  )
}
