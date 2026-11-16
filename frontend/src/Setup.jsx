// One page that says what this workspace still has to connect (ADR-062).
//
// **Why it exists.** Setting this product up meant visiting four screens -- Sending, Job hunt,
// Settings, and a provider's own site -- with nothing saying which of them still needed doing
// or in what order. Every control already existed; what was missing was the list. Found by
// watching somebody try to set it up.
//
// **It owns no control and decides nothing.** Each row links to the screen that does the work,
// and each state is a field that screen's own route already computes. `setupSteps.js` holds the
// decision and its tests; this file draws it.
//
// **Nothing here calls a provider.** Every request is one of our own non-networked reads, so a
// refresh costs a few queries and never a credit or an OAuth token -- the rule that keeps
// `GET /mailboxes` free.

import { Link } from "./router";
import { buildSteps, nextStep, progress } from "./setupSteps";
import { Icons, PageHeader } from "./ui";
import { useLoad } from "./useLoad";

const TONE = { done: "success", attention: "danger" };
const WORD = {
  done: "Done",
  todo: "To do",
  attention: "Needs attention",
  unknown: "Checking",
};

function Step({ step, isNext }) {
  const settled = step.state === "done";
  return (
    <li className="setup-step" data-state={step.state} data-next={isNext || undefined}>
      <span className="setup-mark" aria-hidden="true">
        {settled ? <Icons.check size={14} /> : null}
      </span>
      <div className="setup-body">
        <div className="setup-title">
          <strong>{step.title}</strong>
          <span className="pill" data-tone={TONE[step.state]}>
            {TONE[step.state] ? <span className="pill-dot" /> : null}
            {WORD[step.state]}
          </span>
          {isNext ? (
            <span className="pill" data-tone="brand">
              Start here
            </span>
          ) : null}
        </div>
        <p className="hint">{step.why}</p>
        {step.detail ? <p className="setup-detail">{step.detail}</p> : null}
      </div>
      <Link
        className="btn"
        data-variant={isNext ? "brand" : undefined}
        to={step.to}
      >
        {settled ? "Open" : step.action}
      </Link>
    </li>
  );
}

function Group({ title, note, steps, next }) {
  return (
    <section className="card">
      <h2 className="section-title">{title}</h2>
      <p className="hint" style={{ marginBottom: 12 }}>
        {note}
      </p>
      <ol className="setup-list">
        {steps.map((step) => (
          <Step key={step.id} step={step} isNext={next?.id === step.id} />
        ))}
      </ol>
    </section>
  );
}

export default function Setup({ onError }) {
  // Seven of our own reads, and no provider is called by any of them.
  const mailboxes = useLoad("/mailboxes", onError);
  const sending = useLoad("/admin/sending-config", onError);
  const contactKeys = useLoad("/provider-keys", onError);
  const searchKeys = useLoad("/search-keys", onError);
  const booking = useLoad("/booking-webhook", onError);
  const tokens = useLoad("/mcp-tokens", onError);
  const facts = useLoad("/profile/facts", onError);

  const steps = buildSteps({
    mailboxes,
    sending,
    contactKeys,
    searchKeys,
    booking,
    tokens,
    facts,
  });
  const tally = progress(steps);
  const next = nextStep(steps);
  const percent = tally.total ? (tally.done / tally.total) * 100 : 0;

  return (
    <>
      <PageHeader icon={Icons.check} eyebrow="Controls" title={<>Setup</>}>
        Everything this workspace needs connected, in the order to do it. Each
        row opens the screen that does the work; nothing is changed from here.
      </PageHeader>

      <section className="card setup-summary">
        <div>
          {/* No number until every request has come back: a count stated while something is
              still loading has twice been wrong in this app. */}
          <strong className="setup-count">
            {tally.known
              ? `${tally.done} of ${tally.total} required steps done`
              : "Checking what is connected"}
          </strong>
          <p className="hint">
            {!tally.known
              ? "This reads your own settings and calls no provider."
              : tally.done === tally.total
                ? "This workspace can send. The optional steps below make it more useful."
                : "Until the required steps are done, an approved draft will not be sent."}
          </p>
        </div>
        <div className="progress" aria-hidden="true">
          <span style={{ width: `${tally.known ? percent : 0}%` }} />
        </div>
      </section>

      <Group
        title="Required to send"
        note="Without these, drafts can be written and approved but nothing leaves."
        steps={steps.filter((step) => step.required)}
        next={next}
      />
      <Group
        title="Optional"
        note="Each one adds something; none of them blocks sending."
        steps={steps.filter((step) => !step.required)}
        next={next}
      />
    </>
  );
}
