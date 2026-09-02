import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { ErrorNote } from '@/components/ui'
import { OnAirDot } from '@/components/Layout'
import type { Gender } from '@/types'

const DEMO = [
  { email: 'super@gdclassroom.io', label: 'Nadia · host' },
  { email: 'priya@gdclassroom.io', label: 'Priya' },
  { email: 'arjun@gdclassroom.io', label: 'Arjun' },
  { email: 'meera@gdclassroom.io', label: 'Meera' },
  { email: 'dev@gdclassroom.io', label: 'Dev' },
  { email: 'sana@gdclassroom.io', label: 'Sana' },
]

const GENDERS: { value: Gender; label: string }[] = [
  { value: 'FEMALE', label: 'Female' },
  { value: 'MALE', label: 'Male' },
  { value: 'UNSPECIFIED', label: 'Prefer not to say' },
]

export default function LoginPage() {
  const { login, register } = useAuth()
  const navigate = useNavigate()
  const [joining, setJoining] = useState(false)
  const [email, setEmail] = useState('super@gdclassroom.io')
  const [password, setPassword] = useState('Password123!')
  const [displayName, setDisplayName] = useState('')
  const [gender, setGender] = useState<Gender>('UNSPECIFIED')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (joining) {
        await register({
          email,
          password,
          display_name: displayName.trim(),
          role: 'PARTICIPANT',
          gender,
        })
      }
      const user = await login(email, password)
      navigate(user.role === 'SUPER_USER' ? '/classrooms' : '/invitations')
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : joining
            ? 'Could not create the account.'
            : 'Could not sign in.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-5">
      <div className="w-full max-w-sm animate-rise">
        <div className="mb-7 flex items-center gap-2.5">
          <OnAirDot active />
          <h1 className="text-xl font-bold tracking-tight">AI GD Classroom</h1>
        </div>
        <p className="mb-6 text-sm text-ink-300">
          Voice-first group discussions, moderated end to end by an AI that keeps the
          floor moving and the speaking time even.
        </p>

        <form onSubmit={submit} className="card space-y-4 p-5">
          {joining && (
            <>
              <div>
                <label className="label mb-1.5 block">Your name</label>
                <input
                  className="input"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  placeholder="How your host will know you"
                  minLength={2}
                  required
                />
              </div>
              <div>
                <label className="label mb-1.5 block">Gender</label>
                <div className="flex flex-wrap gap-1.5">
                  {GENDERS.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => setGender(option.value)}
                      className={`pill transition ${
                        gender === option.value
                          ? 'border-signal/60 bg-signal-soft text-signal-deep'
                          : 'border-ink-600 text-ink-300 hover:border-signal'
                      }`}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <p className="mt-1.5 text-xs text-ink-400">
                  Only used to give you a name inside a discussion. Nobody in the room
                  sees your real name.
                </p>
              </div>
            </>
          )}
          <div>
            <label className="label mb-1.5 block">Email</label>
            <input
              className="input"
              type="email"
              value={email}
              autoComplete="username"
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="label mb-1.5 block">Password</label>
            <input
              className="input"
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          <ErrorNote message={error} />

          <button className="btn-primary w-full" disabled={busy}>
            {busy
              ? joining
                ? 'Creating your account…'
                : 'Signing in…'
              : joining
                ? 'Create account'
                : 'Sign in'}
          </button>

          <button
            type="button"
            className="w-full text-center text-xs text-ink-400 hover:text-signal"
            onClick={() => {
              setJoining((was) => !was)
              setError(null)
            }}
          >
            {joining ? 'I already have an account' : 'New here? Create an account'}
          </button>
        </form>

        <div className={`mt-5 ${joining ? 'hidden' : ''}`}>
          <p className="label mb-2">Seeded accounts · password Password123!</p>
          <div className="flex flex-wrap gap-1.5">
            {DEMO.map((account) => (
              <button
                key={account.email}
                type="button"
                onClick={() => setEmail(account.email)}
                className={`pill transition ${
                  email === account.email
                    ? 'border-signal/60 text-signal'
                    : 'border-ink-600 text-ink-300 hover:border-ink-400'
                }`}
              >
                {account.label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
