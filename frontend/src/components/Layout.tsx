import { Link, NavLink, useNavigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useAuth } from '@/store/auth'
import type { StreamStatus } from '@/hooks/useEventStream'

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `rounded-lg px-3 py-1.5 text-sm font-semibold transition ${
    isActive ? 'bg-signal-soft text-signal-deep' : 'text-ink-300 hover:text-ink-100'
  }`

export function Layout({
  children,
  streamStatus,
  pendingCount,
}: {
  children: ReactNode
  streamStatus?: StreamStatus
  pendingCount?: number
}) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-ink-600 bg-ink-800/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-4 px-5">
          <Link to="/" className="flex items-center gap-2.5">
            <OnAirDot active={streamStatus === 'open'} />
            <span className="text-[15px] font-bold tracking-tight">AI GD Classroom</span>
          </Link>

          <nav className="ml-4 flex items-center gap-1">
            {user?.role === 'SUPER_USER' ? (
              <>
                <NavLink to="/classrooms" className={linkClass}>
                  Classrooms
                </NavLink>
                <NavLink to="/llm" className={linkClass}>
                  Routers
                </NavLink>
              </>
            ) : (
              <NavLink to="/invitations" className={linkClass}>
                Invitations
                {pendingCount ? (
                  <span className="ml-2 rounded-full bg-signal px-1.5 text-[11px] text-white">
                    {pendingCount}
                  </span>
                ) : null}
              </NavLink>
            )}
          </nav>

          <div className="ml-auto flex items-center gap-3">
            {streamStatus && streamStatus !== 'open' && (
              <span className="pill border-ink-600 text-ink-300">
                {streamStatus === 'retrying' ? 'reconnecting' : streamStatus}
              </span>
            )}
            <span className="hidden text-sm text-ink-300 sm:block">{user?.display_name}</span>
            <button
              className="btn-ghost !px-3 !py-1.5 text-xs"
              onClick={async () => {
                await logout()
                navigate('/login')
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-5 py-8">{children}</main>
    </div>
  )
}

export function OnAirDot({ active }: { active?: boolean }) {
  return (
    <span className="relative flex h-2.5 w-2.5">
      {active && (
        <span className="absolute inline-flex h-full w-full animate-pulse-ring rounded-full bg-signal" />
      )}
      <span
        className={`relative inline-flex h-2.5 w-2.5 rounded-full ${
          active ? 'bg-signal' : 'bg-ink-500'
        }`}
      />
    </span>
  )
}
