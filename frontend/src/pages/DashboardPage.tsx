import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '@/lib/api'
import { ClassroomBadge, EmptyState, ErrorNote, Spinner } from '@/components/ui'
import type { Classroom, Participant, Topic } from '@/types'

interface Props {
  /** Bumped by the lobby SSE stream whenever anything about a classroom changes. */
  revision: number
}

const SEATS = 4
/** A discussion needs somebody to disagree with; beyond that, whoever the host picks. */
const MIN_INVITEES = 2

export default function DashboardPage({ revision }: Props) {
  const [topics, setTopics] = useState<Topic[]>([])
  const [classrooms, setClassrooms] = useState<Classroom[]>([])
  const [people, setPeople] = useState<Participant[]>([])
  const [selected, setSelected] = useState<string>('')
  const [title, setTitle] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [topicList, roomList, peopleList] = await Promise.all([
        api.get<Topic[]>('/topics'),
        api.get<Classroom[]>('/classrooms?limit=25'),
        api.get<Participant[]>('/users/participants'),
      ])
      setTopics(topicList)
      setClassrooms(roomList)
      setPeople(peopleList)
      setSelected((current) => current || topicList[0]?.id || '')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load your classrooms.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load, revision])

  const ready = Boolean(selected) && picked.length >= MIN_INVITEES

  const create = async () => {
    if (!ready) return
    setCreating(true)
    setError(null)
    try {
      await api.post<Classroom>('/classrooms', {
        topic_id: selected,
        title: title.trim() || null,
        invitee_emails: picked,
      })
      setTitle('')
      setPicked([])
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create the classroom.')
    } finally {
      setCreating(false)
    }
  }

  // Selection is capped at the seat count rather than validated afterwards: a host who
  // has already chosen four should be told by the UI, not by a 422.
  const toggle = (email: string) =>
    setPicked((current) =>
      current.includes(email)
        ? current.filter((item) => item !== email)
        : current.length >= SEATS
          ? current
          : [...current, email],
    )

  const chosen = topics.find((topic) => topic.id === selected)

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-bold tracking-tight">Start a discussion</h1>
        <p className="mt-1 text-sm text-ink-300">
          Pick a topic and choose four people from this app. The invitation shows up in
          their session straight away; the discussion begins once all four accept.
        </p>

        <div className="mt-5 grid gap-3 md:grid-cols-4">
          {topics.map((topic) => (
            <button
              key={topic.id}
              onClick={() => setSelected(topic.id)}
              className={`card p-4 text-left transition ${
                selected === topic.id
                  ? 'border-signal/60 bg-signal/5'
                  : 'hover:border-ink-400'
              }`}
            >
              <div className="label mb-1.5">{topic.slug}</div>
              <div className="text-[15px] font-bold leading-snug">{topic.title}</div>
              <p className="mt-2 line-clamp-3 text-xs leading-relaxed text-ink-300">
                {topic.description}
              </p>
            </button>
          ))}
        </div>

        {chosen && (
          <div className="card mt-4 p-4">
            <div className="label mb-2">Angles the moderator will reach for</div>
            <ul className="grid gap-1.5 text-sm text-ink-200 sm:grid-cols-2">
              {chosen.guiding_points.map((point) => (
                <li key={point} className="flex gap-2">
                  <span className="text-signal">·</span>
                  {point}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="card mt-4 p-4">
          <div className="mb-1 flex items-center justify-between">
            <div className="label">Choose participants</div>
            <span className="font-mono text-[11px] text-ink-400" data-testid="picked-count">
              {picked.length} / {SEATS}
            </span>
          </div>
          <p className="mb-3 text-xs text-ink-400">
            Everyone registered on this app. Pick {MIN_INVITEES} to {SEATS} — the invitation
            appears in their session the moment you create the classroom, with no email.
          </p>

          {people.length === 0 ? (
            <p className="py-4 text-center text-sm text-ink-400">
              Nobody has signed up as a participant yet.
            </p>
          ) : (
            <div className="grid gap-1.5 sm:grid-cols-2">
              {people.map((person) => {
                const on = picked.includes(person.email)
                const full = !on && picked.length >= SEATS
                return (
                  <button
                    key={person.id}
                    type="button"
                    disabled={full}
                    data-testid={`invitee-${person.email}`}
                    onClick={() => toggle(person.email)}
                    className={`flex items-center gap-3 rounded-lg border px-3 py-2 text-left transition ${
                      on
                        ? 'border-signal/60 bg-signal/10'
                        : full
                          ? 'border-ink-700 opacity-40'
                          : 'border-ink-700 hover:border-ink-400'
                    }`}
                  >
                    <span
                      className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ${
                        on ? 'bg-signal text-ink-900' : 'bg-ink-700'
                      }`}
                    >
                      {on ? '✓' : person.display_name.slice(0, 2).toUpperCase()}
                    </span>
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-semibold">
                        {person.display_name}
                      </span>
                      <span className="block truncate font-mono text-[11px] text-ink-400">
                        {person.email}
                      </span>
                    </span>
                  </button>
                )
              })}
            </div>
          )}
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <input
            className="input max-w-xs"
            placeholder="Name this session (optional)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <button className="btn-primary" onClick={create} disabled={creating || !ready}>
            {creating
              ? 'Inviting…'
              : `Create classroom & invite ${picked.length || MIN_INVITEES}`}
          </button>
          {!ready && (
            <span className="text-xs text-ink-400">
              pick at least {MIN_INVITEES - picked.length} more
            </span>
          )}
          <ErrorNote message={error} />
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-bold uppercase tracking-wider text-ink-300">
          Your classrooms
        </h2>

        {loading ? (
          <Spinner label="Loading…" />
        ) : classrooms.length === 0 ? (
          <EmptyState
            title="No classrooms yet"
            body="Create one above and the four people you choose are invited here, in the app, immediately."
          />
        ) : (
          <div className="grid gap-2">
            {classrooms.map((classroom) => (
              <Link
                key={classroom.id}
                to={`/classrooms/${classroom.id}`}
                className="card flex items-center gap-4 px-4 py-3 transition hover:border-ink-400"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold">{classroom.title}</div>
                  <div className="mt-0.5 font-mono text-[11px] text-ink-400">
                    {classroom.topic.title} ·{' '}
                    {new Date(classroom.created_at).toLocaleString()}
                  </div>
                </div>
                <ClassroomBadge status={classroom.status} />
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
