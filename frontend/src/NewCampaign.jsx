// Creating a campaign: the persona, the goal, the schedule and the proof points.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.
//
// The persona form is where hard rule 6 reaches the product rather than the prose: a drafter may
// assert only what maps to a `proof_points` id or to cited research, so this is the list of
// things the system is allowed to claim about the sender.

import { useEffect, useState } from "react";

import { getJson, postJson } from "./api";
import { Field, Icons, ModeOption, PageHeader, TextArea } from "./ui";

export default function NewCampaign({ onError, onToast, onCreated, onCancel }) {
  const [form, setForm] = useState({
    name: "",
    kind: "outreach",
    mode: "research",
    headline: "",
    target_role: "",
    facts: "",
    profile_link: "",
    initial_subject: "",
    initial_body: "",
    followup_subject: "",
    followup_body: "",
    followup_after_days: 3,
    sender_name: "",
    sender_role: "",
    agency_name: "",
    offer: "",
    goal: "book a 20-minute call",
    cta_text: "",
    booking_url: "",
    from_name: "",
    from_email: "",
  });
  const [busy, setBusy] = useState(false);
  const [fieldErrors, setFieldErrors] = useState([]);
  // The accounts this workspace has already connected. Before this the form asked for an address
  // every time, so anybody who meant the account they had connected last week typed it again and
  // got the same row back -- or, worse, a second row they then had to connect separately.
  const [mailboxes, setMailboxes] = useState([]);
  const [chosen, setChosen] = useState("");

  useEffect(() => {
    getJson("/mailboxes")
      .then((data) => {
        setMailboxes(data.mailboxes);
        // Preselect the one that can actually send. With exactly one usable account -- the
        // ordinary case -- there is nothing to decide and nothing to type.
        const usable = data.mailboxes.filter((row) => row.usable);
        if (usable.length === 1) setChosen(usable[0].id);
      })
      // Degrades to the old behaviour rather than erroring: an older server without this route
      // leaves the picker out and the address fields still work.
      .catch(() => setMailboxes([]));
  }, []);

  const set = (key) => (event) =>
    setForm({ ...form, [key]: event.target.value });
  const setMode = (mode) => setForm({ ...form, mode });

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    // The persona validator refuses a campaign with neither a booking link nor an explicit
    // ask, because a draft has to end by asking for something. That rule used to be enforced
    // only on the server, where it arrived as a sentence naming a field this form did not have.
    if (form.mode === "template" && !form.initial_body.trim()) {
      setFieldErrors([
        {
          field: "templates.initial.body",
          message:
            "Write the first email. Use {{first_name}}, {{company}} or any column from your " +
            "sheet where each lead\u2019s own details should go.",
        },
      ]);
      return;
    }
    if (
      form.mode === "research" &&
      !form.cta_text.trim() &&
      !form.booking_url.trim()
    ) {
      setFieldErrors([
        {
          field: "persona.cta_text",
          message:
            "Fill in either a booking link or a call to action. A draft has to end by asking " +
            "for something specific, and this is the sentence it asks with.",
        },
      ]);
      setBusy(false);
      return;
    }
    setFieldErrors([]);
    try {
      // The mailbox first: a campaign can exist without one, but it could never send, and
      // creating them together means one less half-finished state to explain later.
      let mailboxId = chosen || null;
      if (!mailboxId && form.from_email.trim()) {
        const mailbox = await postJson("/mailboxes", {
          json: {
            from_name: form.from_name || form.sender_name,
            from_email: form.from_email,
          },
        });
        mailboxId = mailbox.id;
      }

      const templates =
        form.mode === "template"
          ? {
              initial: {
                subject: form.initial_subject || "Quick question",
                body: form.initial_body,
              },
              // A follow-up is only sent if one was written. `max_followups` defaults to 1, and
              // the shorter of the two wins -- a campaign must never try to send an email that
              // does not exist.
              followups: form.followup_body.trim()
                ? [
                    {
                      subject: form.followup_subject || "Re: {{company}}",
                      body: form.followup_body,
                      after_days: Number(form.followup_after_days) || 3,
                    },
                  ]
                : [],
            }
          : undefined;

      const created = await postJson("/campaigns", {
        json: {
          name: form.name,
          kind: form.kind,
          mode: form.mode,
          ...(templates ? { templates } : {}),
          mailbox_id: mailboxId,
          // A job hunt sends a *candidate profile*, not a persona: a different input form for
          // the same contract. Its facts carry the same `pp1`-style ids, so the claim checker
          // refuses an invented skill exactly as it refuses an invented statistic.
          persona:
            form.kind === "job_hunt"
              ? {
                  kind: "job_hunt",
                  full_name: form.sender_name,
                  headline: form.headline,
                  target_role: form.target_role,
                  facts: form.facts
                    .split(/\r?\n/)
                    .map((line) => line.trim())
                    .filter(Boolean)
                    .map((text, index) => ({ id: `pp${index + 1}`, text })),
                  ...(form.profile_link.trim()
                    ? { links: [form.profile_link.trim()] }
                    : {}),
                  goal: form.goal,
                  cta_text: form.cta_text,
                  ...(form.booking_url.trim()
                    ? { booking_url: form.booking_url.trim() }
                    : {}),
                }
              : {
                  sender_name: form.sender_name,
                  // The rest are what the *research* drafter needs in order to write on your behalf.
                  // A template campaign has no use for them -- you wrote the email -- and somebody
                  // job-hunting has no agency and no offer, so sending empty strings would store a
                  // persona that exists only to get past a form.
                  ...(form.mode === "research"
                    ? {
                        sender_role: form.sender_role,
                        agency_name: form.agency_name,
                        offer: form.offer,
                        goal: form.goal,
                        cta_text: form.cta_text,
                      }
                    : {
                        ...(form.sender_role.trim()
                          ? { sender_role: form.sender_role }
                          : {}),
                        ...(form.agency_name.trim()
                          ? { agency_name: form.agency_name }
                          : {}),
                      }),
                  // Omitted rather than sent empty: the field is a URL or absent, and '' is neither.
                  ...(form.booking_url.trim()
                    ? { booking_url: form.booking_url.trim() }
                    : {}),
                },
        },
      });
      onToast(
        "Campaign created. It starts as a draft and sends nothing until you say so.",
      );
      // The created campaign, so the caller can navigate to it. It used to return nothing and
      // the caller switched to a "campaign" view that then showed whichever campaign happened
      // to be selected -- a redirect to the wrong row.
      onCreated(created ?? null);
    } catch (err) {
      // The API returns which persona field is wrong; showing "invalid" alone would be useless.
      const detail = err?.detail;
      if (detail && Array.isArray(detail.errors)) setFieldErrors(detail.errors);
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeader
        icon={Icons.plus}
        eyebrow="Outreach"
        title={<>New campaign</>}
      >
        A campaign carries the sender persona, the goal and how its emails get
        written, so one tool serves more than one job. The persona is the list
        of claims a draft may make about us — it is validated now rather than at
        drafting time, because a malformed one quietly produces weaker drafts.
      </PageHeader>

      <form className="card" onSubmit={submit}>
        <Field
          label="Campaign name"
          value={form.name}
          onChange={set("name")}
          required
        />

        <h2 className="section-title">What this campaign is for</h2>
        <div className="mode-choice">
          <ModeOption
            value="outreach"
            active={form.kind === "outreach"}
            onChange={(kind) => setForm({ ...form, kind })}
            title="Business outreach"
            note="An agency or team writing to people who could buy. Up to 100 a day, and the
              draft may cite your services and proof points."
          />
          <ModeOption
            value="job_hunt"
            active={form.kind === "job_hunt"}
            onChange={(kind) => setForm({ ...form, kind })}
            title="Job hunting"
            note="One person writing to founders, hiring managers or recruiters. 25 a day by
              default, because a personal Gmail has no domain reputation to spend — and a draft
              may claim nothing that is not in your profile below."
          />
        </div>

        <h2 className="section-title">How the emails get written</h2>
        <div className="mode-choice">
          <ModeOption
            value="research"
            active={form.mode === "research"}
            onChange={setMode}
            title="Research each company"
            note="Reads the company's own site, finds something specific and true, and drafts
              each email around it. Slower and better for agency outreach, where the opening
              line is the whole email."
          />
          <ModeOption
            value="template"
            active={form.mode === "template"}
            onChange={setMode}
            title="One email, personalised"
            note="You write the email once and each lead's own details fill the blanks. No
              research and no model, so a sheet of a thousand drafts in seconds — right for a
              job hunt, or any list that should genuinely get the same message."
          />
        </div>

        {form.mode === "template" ? (
          <>
            <h2 className="section-title">The email</h2>
            <p className="muted">
              Write it once. Put <code>{"{{first_name}}"}</code>,{" "}
              <code>{"{{company}}"}</code> or any column from your sheet where
              that lead's own details belong. A field with no value stops that
              one draft rather than sending a gap — give it a fallback like{" "}
              <code>{"{{first_name|there}}"}</code> if a blank is acceptable.
            </p>
            <Field
              label="Subject"
              value={form.initial_subject}
              onChange={set("initial_subject")}
              hint="Merge fields work here too."
            />
            <TextArea
              label="Body"
              value={form.initial_body}
              onChange={set("initial_body")}
              rows={10}
              required
            />

            <h2 className="section-title">The follow-up</h2>
            <p className="muted">
              Optional. Sent once, three days after the first email by default,
              and only to someone who has not replied — a reply, a bounce or an
              opt-out cancels it.
            </p>
            <Field
              label="Follow-up subject"
              value={form.followup_subject}
              onChange={set("followup_subject")}
              hint="Leave blank for 'Re:' plus the first subject."
            />
            <TextArea
              label="Follow-up body"
              value={form.followup_body}
              onChange={set("followup_body")}
              rows={6}
            />
            <Field
              label="Days after the first email"
              value={form.followup_after_days}
              onChange={set("followup_after_days")}
              type="number"
            />
          </>
        ) : null}

        <h2 className="section-title">Who it comes from</h2>
        {form.mode === "template" ? (
          <p className="muted">
            Your name signs every email and appears in the footer, so a
            recipient can always tell who wrote to them. Nothing else here is
            needed — you wrote the email yourself.
          </p>
        ) : null}
        {form.kind === "job_hunt" ? (
          <>
            <div className="field-grid">
              <Field
                label="Your name"
                value={form.sender_name}
                onChange={set("sender_name")}
                required
              />
              <Field
                label="One line about you"
                value={form.headline}
                onChange={set("headline")}
                required={form.mode === "research"}
                hint="e.g. Backend engineer, 4 years"
              />
              <Field
                label="Portfolio or CV link"
                value={form.profile_link}
                onChange={set("profile_link")}
                hint="A link, not an attachment — cold mail carrying a file lands in spam far
                  more often, and a link costs the reader nothing."
              />
            </div>
            {form.mode === "research" ? (
              <>
                <h2 className="section-title">What you have actually done</h2>
                <p className="muted">
                  One per line.{" "}
                  <strong>A draft may claim nothing that is not here</strong> —
                  no inference, no rounding two years up to three. An email
                  claiming experience you do not have is not a weak email, it is
                  something you will be asked about in an interview.
                </p>
                <TextArea
                  label="Your facts"
                  value={form.facts}
                  onChange={set("facts")}
                  rows={5}
                  required
                  hint="e.g. Four years building Django services at Globex"
                />
                <Field
                  label="What you are looking for"
                  value={form.target_role}
                  onChange={set("target_role")}
                  required
                  hint="e.g. backend roles at early-stage startups"
                />
                <Field
                  label="Goal"
                  value={form.goal}
                  onChange={set("goal")}
                  required
                />
              </>
            ) : null}
          </>
        ) : (
          <div className="field-grid">
            <Field
              label="Your name"
              value={form.sender_name}
              onChange={set("sender_name")}
              required
            />
            <Field
              label="Your role"
              value={form.sender_role}
              onChange={set("sender_role")}
              required={form.mode === "research"}
              hint="e.g. Founder"
            />
            <Field
              label="Agency name"
              value={form.agency_name}
              onChange={set("agency_name")}
              required={form.mode === "research"}
              hint={
                form.mode === "template"
                  ? "Leave blank if you are writing as yourself."
                  : ""
              }
            />
          </div>
        )}

        {form.mode === "research" && form.kind === "outreach" ? (
          <>
            <h2 className="section-title">What you are offering</h2>
            <p className="muted">
              A model writes each email from this, so it has to know what it is
              writing for.
            </p>
            <Field
              label="Offer"
              value={form.offer}
              onChange={set("offer")}
              required
              hint="One line, specific. This is what every draft is built around."
            />
            <Field
              label="Goal"
              value={form.goal}
              onChange={set("goal")}
              required
            />

            <h2 className="section-title">What the email asks for</h2>
            <p className="muted">
              Fill in at least one of these. A cold email that does not end with
              a concrete next step gets read and forgotten, so a campaign
              without one is refused.
            </p>
            <div className="field-grid">
              <Field
                label="Booking link"
                value={form.booking_url}
                onChange={set("booking_url")}
                type="url"
                hint="Your Cal.com or Calendly link. Must start with https://"
              />
              <Field
                label="Call to action"
                value={form.cta_text}
                onChange={set("cta_text")}
                hint="The ask, in your own words — e.g. worth a 20-minute call next week?"
              />
            </div>
          </>
        ) : (
          <>
            <h2 className="section-title">Booking link</h2>
            <p className="muted">
              Optional. If you give one, <code>{"{{booking_url}}"}</code> works
              as a merge field in your template, and it is the only link a draft
              is allowed to contain.
            </p>
            <Field
              label="Booking link"
              value={form.booking_url}
              onChange={set("booking_url")}
              type="url"
              hint="Your public Cal.com or Calendly page — not the booking webhook URL from Sending, which is a secret."
            />
          </>
        )}

        <h2 className="section-title">Sending account</h2>
        <p className="muted">
          Which address this campaign sends from. Pick one you have already
          connected, or add a new one and connect it on the campaign screen
          afterwards.
        </p>
        {mailboxes.length ? (
          <label className="field">
            <span>Send from</span>
            <select
              value={chosen}
              onChange={(event) => setChosen(event.target.value)}
            >
              <option value="">A new address…</option>
              {mailboxes.map((row) => (
                <option key={row.id} value={row.id} disabled={!row.usable}>
                  {row.from_email}
                  {row.usable ? "" : " — not connected yet"}
                  {row.campaigns ? ` · ${row.campaigns} campaign(s)` : ""}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {chosen ? null : (
          <div className="field-grid">
            <Field
              label="Send from (email)"
              value={form.from_email}
              onChange={set("from_email")}
            />
            <Field
              label="Sender name recipients see"
              value={form.from_name}
              onChange={set("from_name")}
              hint="Defaults to your name."
            />
          </div>
        )}

        {fieldErrors.length ? (
          <div className="banner" data-tone="bad" style={{ marginTop: 14 }}>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {fieldErrors.map((item) => (
                <li key={item.field}>
                  <strong>{item.field}</strong>: {item.message}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="btn-row">
          <button
            type="submit"
            className="btn"
            data-variant="primary"
            disabled={busy}
          >
            {busy ? "Creating…" : "Create campaign"}
          </button>
          <button
            type="button"
            className="btn"
            data-variant="quiet"
            onClick={onCancel}
          >
            Cancel
          </button>
        </div>
      </form>
    </>
  );
}

/* ------------------------------------------------------------------- admin */
