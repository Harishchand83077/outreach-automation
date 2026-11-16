// What a workspace still has to connect, decided in one pure function.
//
// **Why this is not a condition inside a component.** The dashboard's suite renders nothing, so
// logic written inside JSX is logic nothing tests -- which is how a button once shipped throwing
// `ReferenceError`. The same bargain as `build.js`: the decision lives here with tests, and
// `Setup.jsx` only draws it.
//
// **Three states, and "unknown" is one of them.** A step whose request has not come back, or
// came back as an error, is `unknown` -- never `todo`. Telling somebody to connect a mailbox
// they connected yesterday because a request was slow is the "No campaign yet" defect again.
//
// **It re-derives nothing.** Every answer below is a field the API already computes -- `usable`
// from `unavailable_reason`, `configured` from the webhook row -- so this cannot drift from the
// screens that own each control. A second copy of "is this mailbox working?" here would be this
// project's signature defect in the one place whose job is to be believed.

/** `done` | `todo` | `attention` | `unknown` */
function state(source, decide) {
  if (!source || !source.asked || source.failed || !source.data) return "unknown";
  return decide(source.data);
}

/**
 * @param sources Each value is a `useLoad` result: `{ data, asked, failed }`.
 * @returns Steps in the order somebody should do them. `required` steps gate sending.
 */
export function buildSteps(sources) {
  const { mailboxes, sending, contactKeys, searchKeys, booking, tokens, facts } =
    sources;

  const boxes = mailboxes?.data?.mailboxes ?? [];
  const lapsed = boxes.filter((box) => box.needs_reconnect).length;

  return [
    {
      id: "mailbox",
      required: true,
      title: "Connect a sending account",
      why: "Nothing can be sent or read without one. Each person sends from their own Gmail or Workspace address; there is no shared account.",
      to: "/sending",
      action: lapsed ? "Reconnect" : "Connect Gmail",
      state: state(mailboxes, (data) => {
        const list = data.mailboxes ?? [];
        if (list.some((box) => box.usable)) return "done";
        return list.some((box) => box.needs_reconnect) ? "attention" : "todo";
      }),
      detail: lapsed
        ? `${lapsed} connection${lapsed === 1 ? " has" : "s have"} lapsed. A lapsed account cannot send and cannot fetch replies.`
        : null,
    },
    {
      id: "send-mode",
      required: true,
      title: "Choose a send mode",
      why: "Sending starts switched off. Allowlist mode sends only to addresses you list, which is how a rehearsal is run; live is the owner's decision.",
      to: "/settings",
      action: "Open settings",
      state: state(sending, (data) =>
        data.send_mode && data.send_mode !== "disabled" ? "done" : "todo",
      ),
      detail:
        sending?.data?.send_mode === "disabled"
          ? "Sending is off, so an approved draft is logged and not sent."
          : null,
    },
    {
      id: "contact-key",
      required: false,
      title: "Add a hiring-contact provider",
      why: "Looks up who to write to at a company, on your own key and your own credits. Hunter's free plan is enough to start.",
      to: "/job-hunt",
      action: "Add a key",
      state: state(contactKeys, (data) =>
        (data.providers ?? []).some(
          (row) => row.usable && row.provider !== "mock",
        )
          ? "done"
          : "todo",
      ),
      detail: null,
    },
    {
      id: "search-key",
      required: false,
      title: "Add your own search key",
      why: "Research falls back to a free tier shared by every workspace. Your own key goes ahead of it, so your research does not depend on other people's usage.",
      to: "/sending",
      action: "Add a key",
      state: state(searchKeys, (data) =>
        (data.providers ?? []).some((row) => row.connected) ? "done" : "todo",
      ),
      detail: null,
    },
    {
      id: "booking",
      required: false,
      title: "Connect your booking tool",
      why: "Lets a booked meeting be recorded when somebody takes a slot, without anybody inferring it from a reply.",
      to: "/sending",
      action: "Set up the webhook",
      state: state(booking, (data) => (data.configured ? "done" : "todo")),
      detail:
        booking?.data?.configured && !booking.data.last_delivery_at
          ? "A secret exists, but no booking has arrived yet. If you have had bookings, check the URL was pasted into the right field."
          : null,
    },
    {
      id: "facts",
      required: false,
      title: "Write your own facts",
      why: "A tailored resume and a fit score are built only from facts you confirmed. With none, there is nothing they are allowed to say.",
      to: "/job-hunt",
      action: "Add facts",
      state: state(facts, (data) =>
        (data.facts ?? []).length > 0 ? "done" : "todo",
      ),
      detail: null,
    },
    {
      id: "connector",
      required: false,
      title: "Connect your own Claude",
      why: "Lets your own assistant read this workspace and propose drafts into the review queue. It can never send or approve.",
      to: "/settings",
      action: "Create a token",
      state: state(tokens, (data) =>
        (data.tokens ?? []).length > 0 ? "done" : "todo",
      ),
      detail: null,
    },
  ];
}

/** How far through the required steps we are, counting only what we actually know. */
export function progress(steps) {
  const required = steps.filter((step) => step.required);
  return {
    done: required.filter((step) => step.state === "done").length,
    total: required.length,
    // Any unknown means the summary is not yet a fact, so the screen must not state one.
    known: steps.every((step) => step.state !== "unknown"),
  };
}

/** The first thing worth doing: a required step that is not done, else an optional one. */
export function nextStep(steps) {
  const open = (step) => step.state === "todo" || step.state === "attention";
  return (
    steps.find((step) => step.required && open(step)) ??
    steps.find((step) => open(step)) ??
    null
  );
}
