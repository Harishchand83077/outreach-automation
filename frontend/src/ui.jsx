/* The small, shared pieces every screen is built from.
 *
 * Kept out of `App.jsx` because that file is the application's behaviour and this is its
 * vocabulary: an icon, a skeleton, an empty state. Mixing them made it hard to see either.
 *
 * No icon library. Twelve hand-written paths cost nothing, ship nothing, and cannot break on a
 * major version bump — and a dependency added for twelve glyphs is a dependency to audit,
 * update and explain for as long as the project lives.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
  );
}

export const Icons = {
  // ---- added in S17.1 -------------------------------------------------------------
  //
  // **Seven of the nine nav items shared three icons** before this: `campaign` was
  // Signals *and* Operations, `inbox` was Sending *and* Admin, `leads` was Leads *and*
  // Job hunt. Two items with one icon is not a small thing on a left rail -- the icon
  // is the control an eye uses to find a screen, so duplicating it means the rail can
  // only be read word by word. Same 24x24 grid and 1.6 stroke as the twelve that were
  // already here, so this is not a second icon vocabulary.
  // Signals: a broadcast, because a signal is something that arrived rather than something we did.
  signal: (p) => (
    <Icon {...p}>
      <path d="M4.93 19.07a10 10 0 0 1 0-14.14" />
      <path d="M7.76 16.24a6 6 0 0 1 0-8.48" />
      <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
      <path d="M16.24 7.76a6 6 0 0 1 0 8.48" />
      <circle cx="12" cy="12" r="1.5" />
    </Icon>
  ),
  // Operations: a pulse, which is what that screen is -- the worker's heartbeat and what failed.
  pulse: (p) => (
    <Icon {...p}>
      <path d="M3 12h4l2.5-7 4 14L16 12h5" />
    </Icon>
  ),
  // Job hunt: a briefcase. The candidate half of the product, and the one icon here anybody
  // would guess from the word.
  briefcase: (p) => (
    <Icon {...p}>
      <rect x="2.5" y="7.5" width="19" height="12" rx="2" />
      <path d="M8.5 7.5V6a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v1.5" />
      <path d="M2.5 12.5h19" />
    </Icon>
  ),
  // Admin: people, because that screen is mostly who may do what.
  people: (p) => (
    <Icon {...p}>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M2.8 20a6.2 6.2 0 0 1 12.4 0" />
      <path d="M16.5 5.2a3.2 3.2 0 0 1 0 5.9" />
      <path d="M18 14.4a6.2 6.2 0 0 1 3.2 5.6" />
    </Icon>
  ),
  // Overview: the home of the product, and the only item above the divider.
  home: (p) => (
    <Icon {...p}>
      <path d="M3.5 10.5 12 3.5l8.5 7" />
      <path d="M5.5 9.5V20h13V9.5" />
      <path d="M10 20v-5.5h4V20" />
    </Icon>
  ),
  // Sending: an envelope with a wing, so it is distinct from `inbox` at 18px.
  send: (p) => (
    <Icon {...p}>
      <path d="M21.5 3.5 2.5 10.2l6.6 2.7" />
      <path d="M21.5 3.5 15 20.5l-5.9-7.6" />
      <path d="M21.5 3.5 9.1 12.9" />
    </Icon>
  ),
  // The graph and the brief: a small constellation. Used for "what we know".
  graph: (p) => (
    <Icon {...p}>
      <circle cx="6" cy="7" r="2.2" />
      <circle cx="18" cy="6" r="2.2" />
      <circle cx="12" cy="17.5" r="2.2" />
      <path d="M7.7 8.6l3.2 6.9" />
      <path d="M16.4 7.6l-3 7.6" />
      <path d="M8.2 6.6h7.6" />
    </Icon>
  ),
  // An external link, for a source somebody is about to open in a new tab.
  external: (p) => (
    <Icon {...p}>
      <path d="M14 4h6v6" />
      <path d="M20 4l-8.5 8.5" />
      <path d="M18 14v4.5a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2H10" />
    </Icon>
  ),
  // Back, for a page header that is one level down.
  back: (p) => (
    <Icon {...p}>
      <path d="M15 5l-7 7 7 7" />
    </Icon>
  ),
  // A chevron, for a breadcrumb separator and a disclosure.
  chevron: (p) => (
    <Icon {...p}>
      <path d="M9 5l7 7-7 7" />
    </Icon>
  ),
  // Sparkle: anything a model did, so "a model wrote this" is visible rather than implied.
  sparkle: (p) => (
    <Icon {...p}>
      <path d="M12 3.5l1.7 4.8 4.8 1.7-4.8 1.7L12 16.5l-1.7-4.8L5.5 10l4.8-1.7z" />
      <path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z" />
    </Icon>
  ),
  // Shield: the safety surfaces -- the send gate, the suppression list, the go-live gate.
  shield: (p) => (
    <Icon {...p}>
      <path d="M12 3.2l7 2.6v5.6c0 4.3-2.9 7.6-7 9.4-4.1-1.8-7-5.1-7-9.4V5.8z" />
      <path d="M9 12l2.2 2.2L15.5 10" />
    </Icon>
  ),
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
  file: (p) => (
    <Icon {...p}>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </Icon>
  ),
  paperclip: (p) => (
    <Icon {...p}>
      <path d="M21 11.5l-8.8 8.8a5 5 0 0 1-7.1-7.1l9-9a3.5 3.5 0 0 1 5 5l-9 9a2 2 0 0 1-2.8-2.8l8.1-8.1" />
    </Icon>
  ),
  // Stacked layers: many workspaces seen from above, which is what the platform scope is.
  platform: (p) => (
    <Icon {...p}>
      <path d="M12 3l9 5-9 5-9-5 9-5z" />
      <path d="M3 13l9 5 9-5" />
    </Icon>
  ),
};

/* ------------------------------------------------------------------ theme */

const THEME_KEY = "outreach.theme";

/** The chosen theme, or `null` meaning "whatever the system says".
 *
 * Stored rather than kept in memory so the choice survives a reload, and applied to
 * `documentElement` rather than passed down, because every rule that depends on it is CSS. */
export function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch {
      // Private browsing, or storage blocked. A theme is a convenience, never a requirement.
      return null;
    }
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme) root.setAttribute("data-theme", theme);
    else root.removeAttribute("data-theme");
    try {
      if (theme) localStorage.setItem(THEME_KEY, theme);
      else localStorage.removeItem(THEME_KEY);
    } catch {
      /* ignored for the same reason as above */
    }
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const resolved =
        current ||
        (window.matchMedia?.("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark");
      return resolved === "dark" ? "light" : "dark";
    });
  }, []);

  return [theme, toggle];
}

export function ThemeToggle({ theme, onToggle }) {
  const resolved =
    theme ||
    (window.matchMedia?.("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark");
  const next = resolved === "dark" ? "light" : "dark";
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
      {resolved === "dark" ? <Icons.sun size={16} /> : <Icons.moon size={16} />}
    </button>
  );
}

/* ------------------------------------------------------------------ states */

/** A placeholder shaped like the thing that is arriving.
 *
 * A spinner says "something is happening"; a skeleton says "a list of four things is
 * happening", which is the part worth knowing while waiting. */
export function Skeleton({ rows = 3, className = "" }) {
  return (
    <div className={`card ${className}`} aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton skeleton-text" />
      ))}
    </div>
  );
}

export function SkeletonList({ count = 3, rows = 3 }) {
  return (
    <div aria-busy="true" aria-live="polite">
      <span className="muted">Loading…</span>
      {Array.from({ length: count }, (_, index) => (
        <Skeleton key={index} rows={rows} className={index ? "" : "first"} />
      ))}
    </div>
  );
}

/** An empty state that says what *would* be here and what to do about it.
 *
 * "No results" is a dead end. A heading, a sentence of cause, and the one action that changes
 * the situation is the difference between a screen that looks broken and one that is simply
 * waiting. */
export function Empty({
  icon: IconComponent = Icons.inbox,
  title,
  children,
  action,
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <IconComponent size={21} />
      </span>
      <strong>{title}</strong>
      {children ? <span>{children}</span> : null}
      {action}
    </div>
  );
}

/* ------------------------------------------------------------------ drawer */

/** A panel beside the page rather than instead of it.
 *
 * Lead detail used to replace the list, so comparing two leads meant navigating twice and
 * losing the scroll position both times. Escape closes, the backdrop closes, and focus moves
 * into the panel on open so a keyboard user is not left behind on the page underneath.
 */
export function Drawer({ title, badges, onClose, children }) {
  const panel = useRef(null);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    // Scrolling the list under an open panel is disorienting, and on a phone it is the whole
    // page moving behind a sheet.
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panel}
      >
        <header className="drawer-head">
          <strong>{title}</strong>
          {badges}
          <span className="spacer" />
          <button
            type="button"
            className="btn"
            data-variant="quiet"
            data-size="icon"
            onClick={onClose}
            aria-label="Close"
            title="Close (Esc)"
          >
            ✕
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}

/* ------------------------------------------------------------------ command palette */

/** Jump anywhere with the keyboard. Cmd/Ctrl+K, type, Enter.
 *
 * The single control that makes a tool feel fast rather than merely complete: every screen and
 * every campaign is two keystrokes away, and nobody has to learn where anything lives.
 *
 * Matching is a plain substring over a lowercase haystack. Fuzzy matching looks clever and
 * ranks badly on short lists — with eight commands and a handful of campaigns, typing "cam"
 * should show the campaigns, not a scored permutation of every item in the app.
 */
export function CommandPalette({ open, onClose, commands }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      // The frame delay matters: focusing before the element is painted silently does nothing.
      requestAnimationFrame(() => input.current?.focus());
    }
  }, [open]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter((command) =>
      `${command.group} ${command.label} ${command.hint || ""}`
        .toLowerCase()
        .includes(needle),
    );
  }, [commands, query]);

  useEffect(() => {
    setActive((current) => Math.min(current, Math.max(matches.length - 1, 0)));
  }, [matches.length]);

  if (!open) return null;

  const run = (command) => {
    onClose();
    command.run();
  };

  const onKeyDown = (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (current + 1) % Math.max(matches.length, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive(
        (current) =>
          (current - 1 + matches.length) % Math.max(matches.length, 1),
      );
    } else if (event.key === "Enter" && matches[active]) {
      event.preventDefault();
      run(matches[active]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  let lastGroup = null;

  return (
    <div className="palette-backdrop" onClick={onClose} role="presentation">
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="palette-input">
          <Icons.search size={17} />
          <input
            ref={input}
            type="text"
            value={query}
            placeholder="Jump to a screen or a campaign…"
            aria-label="Search commands"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
          />
        </div>

        <div className="palette-list">
          {matches.length === 0 ? (
            <p className="muted" style={{ padding: "14px 8px" }}>
              Nothing matches “{query}”.
            </p>
          ) : (
            matches.map((command, index) => {
              const heading =
                command.group !== lastGroup ? command.group : null;
              lastGroup = command.group;
              return (
                <div key={command.id}>
                  {heading ? (
                    <div className="palette-group">{heading}</div>
                  ) : null}
                  <button
                    type="button"
                    className="palette-item"
                    data-active={index === active}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => run(command)}
                  >
                    {command.icon ? <command.icon size={16} /> : null}
                    <span>{command.label}</span>
                    {command.hint ? (
                      <>
                        <span className="spacer" />
                        <span className="badge">{command.hint}</span>
                      </>
                    ) : null}
                  </button>
                </div>
              );
            })
          )}
        </div>

        <div className="palette-foot">
          <span className="kbd">↑</span>
          <span className="kbd">↓</span>
          to move
          <span className="kbd">↵</span>
          to open
          <span className="kbd">esc</span>
          to close
        </div>
      </div>
    </div>
  );
}

/** True while Cmd/Ctrl+K should have the palette open. */
export function usePaletteShortcut() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKey = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((current) => !current);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return [open, setOpen];
}

/* ------------------------------------------------------------------ chart */

/** Sends and replies per day, drawn by hand.
 *
 * A charting library is ~50 KB and a styling argument, for two series of thirty points. This is
 * one `<rect>` per bar, themed by the same tokens as everything else, so it follows the theme
 * toggle for free.
 *
 * Replies are stacked *on* sends rather than drawn beside them: the question is "how much of
 * what went out came back", and two adjacent bars make that a comparison the eye has to do.
 */
export function ActivityChart({ series, loading }) {
  if (loading)
    return (
      <div className="skeleton" style={{ height: 150, borderRadius: 10 }} />
    );
  if (!series?.length) return null;

  const peak = Math.max(
    1,
    ...series.map((point) => point.sent + point.replies),
  );
  const width = 100;
  const height = 34;
  const gap = 0.6;
  const barWidth = width / series.length - gap;
  const totalSent = series.reduce((sum, point) => sum + point.sent, 0);
  const totalReplies = series.reduce((sum, point) => sum + point.replies, 0);

  const label = (iso) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, {
      day: "numeric",
      month: "short",
    });

  return (
    <div className="chart">
      <div className="chart-head">
        <span className="chart-key" style={{ color: "var(--accent)" }}>
          <i />
          <span style={{ color: "var(--text-muted)" }}>{totalSent} sent</span>
        </span>
        <span className="chart-key" style={{ color: "var(--success)" }}>
          <i />
          <span style={{ color: "var(--text-muted)" }}>
            {totalReplies} replied
          </span>
        </span>
        <span className="spacer" />
        <span>last {series.length} days</span>
      </div>

      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${totalSent} sent and ${totalReplies} replies over ${series.length} days`}
      >
        {series.map((point, index) => {
          const x = index * (barWidth + gap);
          const sentHeight = (point.sent / peak) * height;
          const replyHeight = (point.replies / peak) * height;
          return (
            <g key={point.day} className="bar">
              <title>
                {label(point.day)} — {point.sent} sent, {point.replies} replied
              </title>
              {/* A full-height invisible target, so hovering a quiet day still shows its
                  tooltip rather than needing a two-pixel bar hit exactly. */}
              <rect
                x={x}
                y={0}
                width={barWidth}
                height={height}
                fill="transparent"
              />
              <rect
                x={x}
                y={height - sentHeight - replyHeight}
                width={barWidth}
                height={Math.max(sentHeight, point.sent ? 0.7 : 0)}
                fill="var(--accent)"
                rx="0.4"
              />
              <rect
                x={x}
                y={height - replyHeight}
                width={barWidth}
                height={Math.max(replyHeight, point.replies ? 0.7 : 0)}
                fill="var(--success)"
                rx="0.4"
              />
            </g>
          );
        })}
      </svg>

      <div className="chart-axis">
        <span>{label(series[0].day)}</span>
        <span>{label(series[series.length - 1].day)}</span>
      </div>
    </div>
  );
}

/**
 * The top of a page: where you are, what this is, and what you can do from here (S17.2).
 *
 * **Twelve screens each rendered their own `<h1 className="page-title">` followed by a
 * `<p className="page-note">`**, and they had drifted -- some had the paragraph, some did not,
 * none had an eyebrow, and the actions were wherever each screen happened to put them. That is
 * the difference between a set of screens and a product: not any one of them being wrong, but
 * no two of them agreeing on where the title sits.
 *
 * `icon` and `eyebrow` together are what make a page identifiable at a glance when two screens
 * one level apart have similar titles. `actions` sits on the same baseline as the title rather
 * than above or below it, so the eye finds the verb in the same place on every screen.
 */
export function PageHeader({
  icon: IconComponent,
  eyebrow,
  title,
  children,
  actions,
}) {
  return (
    <div className="page-head">
      <div className="page-head-text">
        {eyebrow ? (
          <div className="eyebrow">
            {IconComponent ? <IconComponent size={14} /> : null}
            {eyebrow}
          </div>
        ) : null}
        <h1 className="page-title">{title}</h1>
        {children ? <p className="page-sub">{children}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </div>
  );
}
