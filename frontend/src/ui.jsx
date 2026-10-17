/* The small, shared pieces every screen is built from.
 *
 * Kept out of `App.jsx` because that file is the application's behaviour and this is its
 * vocabulary: an icon, a skeleton, an empty state. Mixing them made it hard to see either.
 *
 * No icon library. Twelve hand-written paths cost nothing, ship nothing, and cannot break on a
 * major version bump — and a dependency added for twelve glyphs is a dependency to audit,
 * update and explain for as long as the project lives.
 */

import { useCallback, useEffect, useState } from 'react'

/* ------------------------------------------------------------------ icons */

function Icon({ children, size = 17, ...rest }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  )
}

export const Icons = {
  review: (p) => (
    <Icon {...p}>
      <path d="M9 11l3 3L22 4" />
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
    </Icon>
  ),
  leads: (p) => (
    <Icon {...p}>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
    </Icon>
  ),
  import: (p) => (
    <Icon {...p}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="M7 10l5 5 5-5M12 15V3" />
    </Icon>
  ),
  campaign: (p) => (
    <Icon {...p}>
      <path d="M3 11l18-8-8 18-2-7-8-3z" />
    </Icon>
  ),
  settings: (p) => (
    <Icon {...p}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6 1.65 1.65 0 0 0 10 3.09V3a2 2 0 1 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.14.35.44.62.8.76" />
    </Icon>
  ),
  check: (p) => (
    <Icon {...p}>
      <path d="M20 6L9 17l-5-5" />
    </Icon>
  ),
  alert: (p) => (
    <Icon {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8v4M12 16h.01" />
    </Icon>
  ),
  info: (p) => (
    <Icon {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16v-4M12 8h.01" />
    </Icon>
  ),
  inbox: (p) => (
    <Icon {...p}>
      <path d="M22 12h-6l-2 3h-4l-2-3H2" />
      <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
    </Icon>
  ),
  sun: (p) => (
    <Icon {...p}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </Icon>
  ),
  moon: (p) => (
    <Icon {...p}>
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </Icon>
  ),
  search: (p) => (
    <Icon {...p}>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </Icon>
  ),
  plus: (p) => (
    <Icon {...p}>
      <path d="M12 5v14M5 12h14" />
    </Icon>
  ),
  quote: (p) => (
    <Icon {...p}>
      <path d="M8 10H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v8a4 4 0 0 1-4 4" />
      <path d="M20 10h-3a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v8a4 4 0 0 1-4 4" />
    </Icon>
  ),
}

/* ------------------------------------------------------------------ theme */

const THEME_KEY = 'outreach.theme'

/** The chosen theme, or `null` meaning "whatever the system says".
 *
 * Stored rather than kept in memory so the choice survives a reload, and applied to
 * `documentElement` rather than passed down, because every rule that depends on it is CSS. */
export function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem(THEME_KEY)
    } catch {
      // Private browsing, or storage blocked. A theme is a convenience, never a requirement.
      return null
    }
  })

  useEffect(() => {
    const root = document.documentElement
    if (theme) root.setAttribute('data-theme', theme)
    else root.removeAttribute('data-theme')
    try {
      if (theme) localStorage.setItem(THEME_KEY, theme)
      else localStorage.removeItem(THEME_KEY)
    } catch {
      /* ignored for the same reason as above */
    }
  }, [theme])

  const toggle = useCallback(() => {
    setTheme((current) => {
      const resolved =
        current ||
        (window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
      return resolved === 'dark' ? 'light' : 'dark'
    })
  }, [])

  return [theme, toggle]
}

export function ThemeToggle({ theme, onToggle }) {
  const resolved =
    theme || (window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
  const next = resolved === 'dark' ? 'light' : 'dark'
  return (
    <button
      type="button"
      className="btn"
      data-variant="quiet"
      data-size="icon"
      onClick={onToggle}
      title={`Switch to ${next} mode`}
      aria-label={`Switch to ${next} mode`}
    >
      {resolved === 'dark' ? <Icons.sun size={16} /> : <Icons.moon size={16} />}
    </button>
  )
}

/* ------------------------------------------------------------------ states */

/** A placeholder shaped like the thing that is arriving.
 *
 * A spinner says "something is happening"; a skeleton says "a list of four things is
 * happening", which is the part worth knowing while waiting. */
export function Skeleton({ rows = 3, className = '' }) {
  return (
    <div className={`card ${className}`} aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton skeleton-text" />
      ))}
    </div>
  )
}

export function SkeletonList({ count = 3, rows = 3 }) {
  return (
    <div aria-busy="true" aria-live="polite">
      <span className="muted">Loading…</span>
      {Array.from({ length: count }, (_, index) => (
        <Skeleton key={index} rows={rows} className={index ? '' : 'first'} />
      ))}
    </div>
  )
}

/** An empty state that says what *would* be here and what to do about it.
 *
 * "No results" is a dead end. A heading, a sentence of cause, and the one action that changes
 * the situation is the difference between a screen that looks broken and one that is simply
 * waiting. */
export function Empty({ icon: IconComponent = Icons.inbox, title, children, action }) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <IconComponent size={21} />
      </span>
      <strong>{title}</strong>
      {children ? <span>{children}</span> : null}
      {action}
    </div>
  )
}
