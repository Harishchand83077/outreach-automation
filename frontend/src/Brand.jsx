// The mark and the wordmark (S17.1).
//
// **The name is drawn from the deployment's own domain rather than invented.** `api.arkvane.in`
// is this system's API and "Arkvane" is the agency name already in the live campaign's persona,
// so that is what the product is called here. It is one constant: change `PRODUCT` and the rail,
// the landing page, the browser tab and the footer all follow. Nothing else in the codebase
// hard-codes a name — a grep for `arkvane` in `app/` returns the API's own hostname and one
// comment about a historical incident, which is what ADR-035 left behind.
//
// The mark is drawn rather than imported for the same reason the twelve icons are: a logo file is
// an asset to keep in sync with a palette, and this one reads its colours from the theme.

export const PRODUCT = "Arkvane";
export const TAGLINE = "Outreach that can show its sources";

/**
 * The mark: two arcs reaching toward a point.
 *
 * It is a send and a reply rather than an envelope, because every tool in this category uses an
 * envelope and the product's actual subject is the round trip -- something goes out, something
 * comes back, and the system is the part in the middle that keeps them matched.
 */
export function Mark({ size = 28, title }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      role={title ? "img" : "presentation"}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      <defs>
        {/* The one gradient in the app that is not a CSS token, because an SVG fill cannot read
            one. The stops are the same two brand hues; if those change, these change with them. */}
        <linearGradient id="ark-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="var(--brand)" />
          <stop offset="65%" stopColor="var(--brand-strong)" />
          <stop offset="100%" stopColor="var(--brand-2)" />
        </linearGradient>
      </defs>
      <rect x="0" y="0" width="32" height="32" rx="9" fill="url(#ark-mark)" />
      <path
        d="M9 21.5 16 9l7 12.5"
        fill="none"
        stroke="var(--brand-contrast)"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M12.4 18.2h7.2"
        fill="none"
        stroke="var(--brand-contrast)"
        strokeWidth="2.4"
        strokeLinecap="round"
        opacity="0.75"
      />
    </svg>
  );
}

/** Mark plus name. `compact` drops the name, for the rail when it is a horizontal strip. */
export function Wordmark({ size = 28, compact = false }) {
  return (
    <>
      <Mark size={size} title={PRODUCT} />
      {compact ? null : (
        <span
          style={{
            fontSize: "var(--text-lg)",
            fontWeight: 700,
            letterSpacing: "-0.02em",
          }}
        >
          {PRODUCT}
        </span>
      )}
    </>
  );
}
