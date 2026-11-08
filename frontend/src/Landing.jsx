// The signed-out page: the first screen anybody is ever shown (S17.1).
//
// **Every claim on this page is true of the code, and each one is checkable.** That is not a
// style note -- this is the page the product is pitched from, and a landing page that overstates
// what the system does is the same defect as a draft that invents a statistic, aimed at a
// different reader. Where a number appears it is the default in the code, not a round figure:
//
// - "a person approves every email" -- `CampaignSettings.approval_mode` defaults to
//   `review_all`, and `tests/unit/test_send_guard.py` asserts structurally over the whole tree
//   that exactly one function decides whether anything reaches SMTP.
// - "every claim resolves to a page we read" -- the drafter may assert only what maps to a
//   persona proof point or a verified research hook, and `_check_claims_resolve` refuses the
//   draft otherwise (hard rule 6).
// - "pauses itself above a 5% bounce rate over 20 sends" -- `MAX_BOUNCE_RATE = 0.05` and
//   `MIN_SENDS = 20` in `services/deliverability/health.py` (ADR-034).
// - "100 a day per mailbox, warming up from 20" -- `daily_cap` defaults to 100 and the warm-up
//   ramp starts at 20, +10 every two business days.
//
// If any of those change, this page changes with them. There is no number here that nothing
// enforces.

import { Icons } from "./ui";
import { Mark, PRODUCT } from "./Brand";
import { SignIn } from "./Auth";

const POINTS = [
  {
    title: "Every line can show its source",
    body:
      "A draft may only assert what resolves to a page we actually read or a proof point you " +
      "wrote. Anything else fails validation instead of being sent.",
  },
  {
    title: "A person approves every email",
    body:
      "Nothing leaves without a human click, and exactly one function in the codebase decides " +
      "whether anything reaches a mail server at all.",
  },
  {
    title: "It stops itself before a provider does",
    body:
      "Above a 5% bounce rate over a meaningful number of sends, the mailbox pauses on its own " +
      "and says why. Nothing automatic can ever widen sending — only restrict it.",
  },
  {
    title: "A reason to write today",
    body:
      "Public hiring boards are read once a day, so a company that just opened three " +
      "engineering roles shows up as a reason to write to them this week.",
  },
];

export default function Landing() {
  return (
    <div className="landing">
      <header className="landing-bar">
        <Mark size={30} title={PRODUCT} />
        <strong
          style={{ fontSize: "var(--text-lg)", letterSpacing: "-0.02em" }}
        >
          {PRODUCT}
        </strong>
        <span
          className="pill"
          data-tone="accent"
          style={{ marginLeft: "var(--space-2)" }}
        >
          <span className="pill-dot" />
          Private beta
        </span>
      </header>

      <main className="landing-main">
        <div>
          <div className="eyebrow">
            <Icons.shield size={14} />
            Grounded outreach
          </div>
          <h1 className="hero-title">
            Cold outreach that can <em>show its sources</em>.
          </h1>
          <p className="hero-sub">
            {PRODUCT} researches a company from pages it actually read, drafts
            an email that may only say what those pages support, and waits for
            you to approve it. Then it handles the replies, the bounces and the
            opt-outs until every lead reaches an end.
          </p>

          <ul className="hero-points">
            {POINTS.map((point) => (
              <li key={point.title}>
                <Icons.check size={18} />
                <span>
                  <strong>{point.title}.</strong>{" "}
                  <span style={{ color: "var(--text-muted)" }}>
                    {point.body}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>

        {/* The sign-in form, unchanged in behaviour: a magic link and no password field
            anywhere. It is inside a card here rather than alone on a grey page, which is the
            whole difference between a login screen and a front door. */}
        {/* The card is the panel, so `SignIn` renders without one of its own -- and still
            owns its heading, because in a half-built deployment that heading reads
            "Authentication is not configured", which is the one sentence somebody needs. */}
        <div className="hero-card">
          <SignIn embedded />
        </div>
      </main>

      <footer className="landing-foot">
        <div className="landing-foot-inner">
          <Mark size={18} />
          <span>
            {PRODUCT} — 100 emails a day per mailbox, warming up from 20.
            Mon–Fri, 10:00–17:30 IST. Plain text, no tracking pixels, no link
            rewriting.
          </span>
        </div>
      </footer>
    </div>
  );
}
