// The job-hunt half of the product, which until now had no screen at all.
//
// Phases 12 and 13 built F5 (job postings and a daily board poll), F6 (a candidate's own
// confirmed facts and answer bank), F9 (hiring-contact lookups on the user's own provider key),
// F10 (referrals from their own LinkedIn export) and F20 (tailored resumes and an application
// tracker) -- twenty-odd endpoints, every one of them reachable only with curl. That is the same
// failure as Phase 8's: the system was deployed, correct, and unusable, and only opening it
// showed that.
//
// Four rules this screen keeps, because each one is a product rule rather than a layout choice:
//
//   * **Nothing here sends anything.** No button on this screen contacts anybody. The referral
//     note is text to copy; the contact lookup writes a proposal; marking an application applied
//     records what the person did themselves.
//   * **A proposal is not a fact.** A fact mined from a resume or GitHub is shown as a proposal
//     until the person confirms it, because the person asked about a claim in an interview is the
//     one who has to have agreed to it.
//   * **Spending is a button press.** A contact lookup spends the user's own provider credit, so
//     it never happens on load, on a refresh or on a tab change.
//   * **An applied application cannot go back.** The API refuses it; the UI does not offer it.

import { useCallback, useEffect, useState } from "react";

import {
  deleteJson,
  getJson,
  patchJson,
  postJson,
  putJson,
  saveAs,
  uploadFile,
} from "./api";
import { useLoad } from "./useLoad";
import { Drawer, Empty, Icons, SkeletonList } from "./ui";

const TABS = [
  { id: "postings", label: "Postings" },
  { id: "profile", label: "Your facts" },
  { id: "resume", label: "Resume & applications" },
  { id: "network", label: "Referrals" },
  { id: "contacts", label: "Hiring contacts" },
];

function Field({ label, hint, children }) {
  // A `div` wrapping a `label`, not a `label` wrapping everything: `app.css` styles
  // `.field label`, and a `span` would not match it. Clicking the text still focuses the
  // control because `.form-label` is a real label element.
  return (
    <div className="field">
      <label className="form-label">{label}</label>
      {children}
      {hint ? <p className="hint">{hint}</p> : null}
    </div>
  );
}

// Every panel loads the same way: ask, and treat an error as *loaded* rather than leaving a
// skeleton spinning above a real problem. That bug shipped once already.

function Loading({ failed, data, what }) {
  if (failed) return <p className="muted">Could not load {what}.</p>;
  if (!data) return <SkeletonList count={2} rows={2} />;
  return null;
}

function Postings({ onError, onToast, mayAdminister }) {
  const posts = useLoad("/job-posts", onError);
  // Scores by posting id, loaded only for the posting somebody asked about. **Scoring calls a
  // model**, so it never happens on load or on a tab change -- the same rule that keeps a
  // contact lookup from spending a credit when somebody presses F5.
  const [fits, setFits] = useState({});
  const [scoring, setScoring] = useState(null);
  const boards = useLoad("/watched-boards", onError);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [bodyText, setBodyText] = useState("");
  const [source, setSource] = useState("greenhouse");
  const [slug, setSlug] = useState("");
  const [busy, setBusy] = useState(false);

  const paste = (event) => {
    event.preventDefault();
    setBusy(true);
    postJson("/job-posts", {
      json: { url, title: title || null, body_text: bodyText || null },
    })
      .then(() => {
        setUrl("");
        setTitle("");
        setBodyText("");
        onToast("Posting saved.");
        posts.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  const watch = (event) => {
    event.preventDefault();
    setBusy(true);
    postJson("/watched-boards", { json: { source, slug } })
      .then(() => {
        setSlug("");
        onToast("Board added. It is polled once a day.");
        boards.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  return (
    <>
      <section className="card">
        <h2>Paste a posting</h2>
        <p className="muted">
          Nothing here fetches the page you are reading. Boards with no public
          feed — Naukri, Instahyre, a LinkedIn post — are supported by you
          supplying the text, which is what keeps this on the right side of each
          board&apos;s terms.
        </p>
        <form onSubmit={paste}>
          <Field label="Posting URL">
            <input
              type="url"
              required
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://acme.example/careers/backend-engineer"
            />
          </Field>
          <Field
            label="Title"
            hint="Optional. Taken from the page if you paste the text."
          >
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
            />
          </Field>
          <Field
            label="The posting text"
            hint="Optional, and the only way we ever see a description for a board with no feed."
          >
            <textarea
              rows={5}
              value={bodyText}
              onChange={(event) => setBodyText(event.target.value)}
            />
          </Field>
          <button
            type="submit"
            className="btn"
            data-variant="primary"
            disabled={busy || !url}
          >
            Save posting
          </button>
        </form>
      </section>

      <section className="card">
        <h2>Watched boards</h2>
        <p className="muted">
          A public ATS feed, polled once a day. Postings already stored stay if
          you stop watching: you may have applied to one.
        </p>
        <Loading {...boards} what="the watched boards" />
        {boards.data ? (
          boards.data.watched_boards.length === 0 ? (
            <Empty icon={Icons.inbox} title="No boards watched yet">
              Add a Greenhouse, Lever or Ashby slug and new postings arrive by
              themselves.
            </Empty>
          ) : (
            <ul className="list">
              {boards.data.watched_boards.map((board) => (
                <li key={board.id} className="list-row">
                  <div>
                    <strong>{board.label || board.slug}</strong>{" "}
                    <span className="badge">{board.source}</span>
                    <div className="hint">
                      {board.last_polled_at
                        ? `Last polled ${new Date(board.last_polled_at).toLocaleString()}`
                        : "Not polled yet"}
                      {board.last_error ? ` — ${board.last_error}` : ""}
                    </div>
                  </div>
                  {mayAdminister ? (
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        deleteJson(`/watched-boards/${board.id}`)
                          .then(() => {
                            onToast("Stopped watching.");
                            boards.reload();
                          })
                          .catch(onError)
                      }
                    >
                      Stop watching
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          )
        ) : null}

        {mayAdminister ? (
          <form onSubmit={watch} style={{ marginTop: 14 }}>
            <Field label="Board">
              <select
                value={source}
                onChange={(event) => setSource(event.target.value)}
              >
                {(boards.data?.sources || ["greenhouse", "lever", "ashby"]).map(
                  (name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ),
                )}
              </select>
            </Field>
            <Field
              label="Slug"
              hint="The company's own name in the board URL — jobs.lever.co/acme is `acme`."
            >
              <input
                value={slug}
                onChange={(event) => setSlug(event.target.value)}
              />
            </Field>
            <button type="submit" className="btn" disabled={busy || !slug}>
              Watch this board
            </button>
          </form>
        ) : null}
      </section>

      <section className="card">
        <h2>Postings</h2>
        <Loading {...posts} what="the postings" />
        {posts.data ? (
          posts.data.job_posts.length === 0 ? (
            <Empty icon={Icons.inbox} title="No postings yet">
              Paste one above, or watch a board and wait for the daily poll.
            </Empty>
          ) : (
            <ul className="list">
              {posts.data.job_posts.map((post) => (
                <li key={post.id} className="list-row">
                  <div>
                    <strong>{post.title}</strong>{" "}
                    <span className="badge">{post.source}</span>
                    <div className="hint">
                      {post.location || "Location not given"}
                      {/* A feed's apply link is the board's, so there is often no company
                          domain to show. Saying so is better than showing the board's host as
                          if it were the employer, which is what this column used to hold. */}
                      {post.company_domain ? ` — ${post.company_domain}` : ""}
                      {post.has_body ? "" : " — no description in the feed"}
                    </div>
                    {/* **Never a bare number.** The score is the fraction of the posting's own
                        requirements that a confirmed fact matched, and each match names the
                        fact and the words that made it one. A score on its own invites whoever
                        reads it to repeat it as a judgement about themselves. */}
                    {fits[post.id]?.fit ? (
                      <details className="evidence">
                        <summary>
                          Fit: {Math.round(fits[post.id].fit.score * 100)}% —{" "}
                          {fits[post.id].note}
                        </summary>
                        <div className="evidence-body">
                          {(fits[post.id].fit.matched || []).map(
                            (match, index) => (
                              <p key={index} className="hint">
                                <strong>{match.requirement}</strong> — matched{" "}
                                {match.fact_id} on{" "}
                                {(match.shared || []).join(", ")}
                              </p>
                            ),
                          )}
                          {(fits[post.id].fit.missing || []).length ? (
                            <p className="hint">
                              <strong>Not mentioned in your facts:</strong>{" "}
                              {fits[post.id].fit.missing.join("; ")}
                            </p>
                          ) : null}
                        </div>
                      </details>
                    ) : null}
                  </div>
                  <div className="btn-row">
                    <a
                      className="btn"
                      href={post.apply_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open
                    </a>
                    <button
                      type="button"
                      className="btn"
                      disabled={scoring === post.id}
                      onClick={() => {
                        setScoring(post.id);
                        postJson(`/job-posts/${post.id}/fit`)
                          .then((body) =>
                            setFits((all) => ({ ...all, [post.id]: body })),
                          )
                          .catch(onError)
                          .finally(() => setScoring(null));
                      }}
                    >
                      {scoring === post.id ? "Scoring..." : "Score the fit"}
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        postJson(`/job-posts/${post.id}/tailor`, { json: {} })
                          .then((body) => onToast(body.note))
                          .catch(onError)
                      }
                    >
                      Tailor a resume
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        postJson("/applications", {
                          json: { job_post_id: post.id },
                        })
                          .then(() => onToast("Tracking it as planned."))
                          .catch(onError)
                      }
                    >
                      Track
                    </button>
                    {mayAdminister ? (
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          // Archived rather than deleted: an application may point at it.
                          deleteJson(`/job-posts/${post.id}`)
                            .then(() => {
                              onToast("Archived.");
                              posts.reload();
                            })
                            .catch(onError)
                        }
                      >
                        Archive
                      </button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </section>
    </>
  );
}

function Profile({ onError, onToast }) {
  const facts = useLoad("/profile/facts", onError);
  const answers = useLoad("/profile/answers", onError);
  const [kind, setKind] = useState("");
  const [statement, setStatement] = useState("");
  const [evidenceUrl, setEvidenceUrl] = useState("");
  const [pattern, setPattern] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);

  const kinds = facts.data?.kinds || [];
  const chosenKind = kind || kinds[0] || "";

  const addFact = (event) => {
    event.preventDefault();
    setBusy(true);
    postJson("/profile/facts", {
      json: { kind: chosenKind, statement, evidence_url: evidenceUrl || null },
    })
      .then(() => {
        setStatement("");
        setEvidenceUrl("");
        onToast("Fact added, and confirmed because you typed it.");
        facts.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  const addAnswer = (event) => {
    event.preventDefault();
    setBusy(true);
    postJson("/profile/answers", {
      json: { question_pattern: pattern, answer, approve: false },
    })
      .then(() => {
        setPattern("");
        setAnswer("");
        onToast("Saved. Approve it before it is ever offered.");
        answers.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  return (
    <>
      <section className="card">
        <h2>Your facts</h2>
        <p className="muted">
          {facts.data?.note || "Only a confirmed fact may be claimed."}
        </p>
        <Loading {...facts} what="your facts" />
        {facts.data ? (
          facts.data.facts.length === 0 ? (
            <Empty icon={Icons.inbox} title="No facts yet">
              A draft and a tailored resume may claim only what is here, so this
              is the one screen that has to be filled in before either is any
              use.
            </Empty>
          ) : (
            <ul className="list">
              {facts.data.facts.map((fact) => (
                <li key={fact.id} className="list-row">
                  <div>
                    <strong>{fact.fact_id}</strong>{" "}
                    <span className="badge">{fact.kind}</span>
                    {fact.confirmed ? null : (
                      <span className="badge">proposal</span>
                    )}
                    <div>{fact.statement}</div>
                    {fact.evidence_url ? (
                      <a
                        className="hint"
                        href={fact.evidence_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {fact.evidence_url}
                      </a>
                    ) : null}
                  </div>
                  <div className="btn-row">
                    {fact.confirmed ? null : (
                      <button
                        type="button"
                        className="btn"
                        data-variant="primary"
                        onClick={() =>
                          postJson(`/profile/facts/${fact.id}/confirm`, {
                            json: {},
                          })
                            .then(() => {
                              onToast("Confirmed.");
                              facts.reload();
                            })
                            .catch(onError)
                        }
                      >
                        Confirm
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        deleteJson(`/profile/facts/${fact.id}`)
                          .then(() => {
                            onToast("Removed.");
                            facts.reload();
                          })
                          .catch(onError)
                      }
                    >
                      Remove
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : null}

        <form onSubmit={addFact} style={{ marginTop: 14 }}>
          <Field label="Kind">
            <select
              value={chosenKind}
              onChange={(event) => setKind(event.target.value)}
            >
              {kinds.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="The fact, in your own words"
            hint="This is what a resume line or an email may assert. Write it as you would say it in an interview."
          >
            <textarea
              rows={3}
              value={statement}
              onChange={(event) => setStatement(event.target.value)}
            />
          </Field>
          <Field
            label="Evidence URL"
            hint="Optional: a repository, a release, a paper."
          >
            <input
              type="url"
              value={evidenceUrl}
              onChange={(event) => setEvidenceUrl(event.target.value)}
            />
          </Field>
          <button
            type="submit"
            className="btn"
            data-variant="primary"
            disabled={busy || !statement || !chosenKind}
          >
            Add fact
          </button>
        </form>
      </section>

      <section className="card">
        <h2>Answer bank</h2>
        <p className="muted">
          Answers to the questions application forms keep asking. Only an
          approved answer is ever offered, and editing one unapproves it — the
          approval was of the previous wording.
        </p>
        <Loading {...answers} what="your answers" />
        {answers.data ? (
          answers.data.answers.length === 0 ? (
            <Empty icon={Icons.inbox} title="No answers yet">
              The browser extension asks this list, and reports a question it
              cannot match rather than guessing at one.
            </Empty>
          ) : (
            <ul className="list">
              {answers.data.answers.map((row) => (
                <li key={row.id} className="list-row">
                  <div>
                    <strong>{row.question_pattern}</strong>
                    {row.approved ? null : (
                      <span className="badge">not approved</span>
                    )}
                    <div className="hint">{row.answer}</div>
                  </div>
                  {row.approved ? null : (
                    <button
                      type="button"
                      className="btn"
                      data-variant="primary"
                      onClick={() =>
                        postJson(`/profile/answers/${row.id}/approve`, {
                          json: {},
                        })
                          .then(() => {
                            onToast("Approved.");
                            answers.reload();
                          })
                          .catch(onError)
                      }
                    >
                      Approve
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )
        ) : null}

        <form onSubmit={addAnswer} style={{ marginTop: 14 }}>
          <Field
            label="The question, as forms ask it"
            hint="Matched loosely, so “Why do you want to work here?” covers the usual variations."
          >
            <input
              value={pattern}
              onChange={(event) => setPattern(event.target.value)}
            />
          </Field>
          <Field label="Your answer">
            <textarea
              rows={4}
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
            />
          </Field>
          <button
            type="submit"
            className="btn"
            disabled={busy || !pattern || !answer}
          >
            Save answer
          </button>
        </form>
      </section>
    </>
  );
}

function ResumeAndApplications({ onError, onToast }) {
  const versions = useLoad("/resume-versions", onError);
  const applications = useLoad("/applications", onError);
  const [open, setOpen] = useState(null);

  // **A version has to be readable before it is attached anywhere**, which is ADR-043's own
  // argument: the checks catch an invented number or skill, and what they cannot catch is a
  // lower-case exaggeration -- so a person reads it. A list of labels with a download button
  // is not reading it.
  const show = (id) =>
    getJson(`/resume-versions/${id}`).then(setOpen).catch(onError);

  return (
    <>
      <section className="card">
        <h2>Resume versions</h2>
        <p className="muted">{versions.data?.note || ""}</p>
        <Loading {...versions} what="your resume versions" />
        {versions.data ? (
          versions.data.versions.length === 0 ? (
            <Empty icon={Icons.inbox} title="No versions yet">
              Press <strong>Tailor a resume</strong> on a posting. A proposal is
              assembled from your confirmed facts, and you see it before
              anything is attached anywhere.
            </Empty>
          ) : (
            <ul className="list">
              {versions.data.versions.map((version) => (
                <li key={version.id} className="list-row">
                  <div>
                    <strong>{version.label}</strong>
                    <div className="hint">
                      {version.created_by ? `by ${version.created_by}` : ""}
                      {version.created_at
                        ? ` — ${new Date(version.created_at).toLocaleString()}`
                        : ""}
                    </div>
                  </div>
                  <div className="btn-row">
                    <button
                      type="button"
                      className="btn"
                      onClick={() => show(version.id)}
                    >
                      Read it
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        postJson(`/resume-versions/${version.id}/render`, {
                          json: {},
                        })
                          .then(() => onToast("Rendering the PDF."))
                          .catch(onError)
                      }
                    >
                      Render PDF
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        saveAs(
                          `/resume-versions/${version.id}/pdf`,
                          `${version.label || "resume"}.pdf`,
                        ).catch(onError)
                      }
                    >
                      Download
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </section>

      {open ? (
        <Drawer
          title={open.version.label}
          badges={[
            open.version.created_by ? `by ${open.version.created_by}` : null,
            open.pdf ? "PDF rendered" : "no PDF yet",
          ].filter(Boolean)}
          onClose={() => setOpen(null)}
        >
          <p className="hint">
            Every line below is one of your confirmed facts, in the order
            chosen. A rewording may not assert anything its fact does not — but
            no check catches a lower-case exaggeration, so read it as the person
            who will be asked about it.
          </p>
          <pre className="snippet">
            {open.version.rendered_text || "(no text stored)"}
          </pre>
        </Drawer>
      ) : null}

      <section className="card">
        <h2>Applications</h2>
        <p className="muted">
          A tracker, and the one thing it must never do is forget that you sent
          a document to a company — so an applied row cannot go back to planned,
          and only you can mark one applied.
        </p>
        <Loading {...applications} what="your applications" />
        {applications.data ? (
          applications.data.applications.length === 0 ? (
            <Empty icon={Icons.inbox} title="Nothing tracked yet">
              Press <strong>Track</strong> on a posting. It starts as planned,
              which is not a claim that anybody applied.
            </Empty>
          ) : (
            <ul className="list">
              {applications.data.applications.map((row) => (
                <li key={row.id} className="list-row">
                  <div>
                    <span className="badge">{row.status}</span>
                    <div className="hint">
                      {row.applied_at
                        ? `Applied ${new Date(row.applied_at).toLocaleDateString()}`
                        : "Not applied yet"}
                      {row.notes ? ` — ${row.notes}` : ""}
                    </div>
                  </div>
                  <div className="btn-row">
                    {(applications.data.statuses || [])
                      // Planned is deliberately absent once a row has moved on: the API refuses
                      // applied -> planned, so offering it would be a button that always fails.
                      .filter(
                        (name) =>
                          name !== row.status &&
                          !(row.applied_at && name === "planned"),
                      )
                      .map((name) => (
                        <button
                          key={name}
                          type="button"
                          className="btn"
                          onClick={() =>
                            patchJson(`/applications/${row.id}`, {
                              json: { new_status: name },
                            })
                              .then(() => {
                                onToast(`Moved to ${name}.`);
                                applications.reload();
                              })
                              .catch(onError)
                          }
                        >
                          {name}
                        </button>
                      ))}
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </section>
    </>
  );
}

function Network({ onError, onToast }) {
  const summary = useLoad("/connections", onError);
  const [company, setCompany] = useState("");
  const [senderName, setSenderName] = useState("");
  const [found, setFound] = useState(null);
  const [busy, setBusy] = useState(false);

  const upload = (file) => {
    if (!file) return;
    setBusy(true);
    uploadFile("/connections/import", file)
      .then((body) => {
        onToast(`Stored ${body.stored}, updated ${body.updated}.`);
        summary.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  const search = (event) => {
    event.preventDefault();
    setBusy(true);
    const query = new URLSearchParams({ company, sender_name: senderName });
    getJson(`/referrals?${query}`)
      .then(setFound)
      .catch(onError)
      .finally(() => setBusy(false));
  };

  return (
    <>
      <section className="card">
        <h2>Your connections</h2>
        <p className="muted">
          Upload <code>Connections.csv</code> from your own LinkedIn data
          export. Nothing here is ever fetched from LinkedIn, and the
          export&apos;s email column is discarded before anything is written —
          this is for matching companies, not a contact database.
        </p>
        <Loading {...summary} what="your connections" />
        {summary.data ? (
          <p>
            <strong>{summary.data.connections}</strong> connections stored.
          </p>
        ) : null}
        <div className="btn-row">
          <label className="btn">
            Upload export
            <input
              type="file"
              accept=".csv"
              style={{ display: "none" }}
              onChange={(event) => upload(event.target.files?.[0])}
            />
          </label>
          {summary.data?.connections ? (
            <button
              type="button"
              className="btn"
              data-variant="danger"
              onClick={() =>
                deleteJson("/connections")
                  .then(() => {
                    onToast("Forgotten.");
                    setFound(null);
                    summary.reload();
                  })
                  .catch(onError)
              }
            >
              Forget all
            </button>
          ) : null}
        </div>
      </section>

      <section className="card">
        <h2>Who do I know there?</h2>
        <form onSubmit={search}>
          <Field label="Company">
            <input
              value={company}
              onChange={(event) => setCompany(event.target.value)}
              placeholder="Acme Analytics"
            />
          </Field>
          <Field
            label="Sign the note as"
            hint="Optional. Left blank, the note ends “Thanks,”."
          >
            <input
              value={senderName}
              onChange={(event) => setSenderName(event.target.value)}
            />
          </Field>
          <button
            type="submit"
            className="btn"
            data-variant="primary"
            disabled={busy || !company}
          >
            Search my network
          </button>
        </form>

        {found ? (
          found.referrals.length === 0 ? (
            <Empty icon={Icons.inbox} title={`Nobody matched ${found.company}`}>
              Names are matched on the company each connection typed on their
              own profile, so a subsidiary or an old employer may be written
              differently.
            </Empty>
          ) : (
            <>
              <p className="muted">{found.note}</p>
              <ul className="list">
                {found.referrals.map((row) => (
                  <li key={row.connection_id} className="list-row is-stacked">
                    <strong>{row.full_name}</strong>
                    <div className="hint">
                      {row.position || "No title recorded"} — {row.company} (
                      {row.why})
                    </div>
                    <textarea
                      readOnly
                      rows={6}
                      value={row.note}
                      style={{ marginTop: 8 }}
                    />
                    <div className="btn-row">
                      <button
                        type="button"
                        className="btn"
                        onClick={() => {
                          navigator.clipboard?.writeText(row.note);
                          onToast("Copied. Send it yourself on LinkedIn.");
                        }}
                      >
                        Copy note
                      </button>
                      {row.profile_url ? (
                        <a
                          className="btn"
                          href={row.profile_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Open profile
                        </a>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )
        ) : null}
      </section>
    </>
  );
}

function Contacts({ campaign, onError, onToast, mayAdminister }) {
  const keys = useLoad("/provider-keys", onError);
  const candidates = useLoad("/contact-candidates", onError);
  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [clientId, setClientId] = useState("");
  const [domain, setDomain] = useState("");
  const [lookupWith, setLookupWith] = useState("");
  const [busy, setBusy] = useState(false);

  const providers = keys.data?.providers || [];
  const usable = providers.filter((row) => row.connected && row.usable);

  const save = (event) => {
    event.preventDefault();
    setBusy(true);
    const body = { api_key: apiKey };
    if (clientId) body.client_id = clientId;
    // PUT, so saving again replaces rather than adding: one account per provider per org is
    // hard rule 8 expressed in the write path.
    putJson(`/provider-keys/${provider || providers[0]?.provider || ""}`, {
      json: body,
    })
      .then(() => {
        setApiKey("");
        setClientId("");
        onToast("Key saved. It is never shown again.");
        keys.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  const lookup = (event) => {
    event.preventDefault();
    setBusy(true);
    postJson("/contact-candidates/lookup", {
      json: { provider: lookupWith, domain },
    })
      .then((body) => {
        onToast(
          `${body.proposed?.length ?? 0} proposed, ${body.already_known ?? 0} already known.`,
        );
        candidates.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  return (
    <>
      <section className="card">
        <h2>Your provider keys</h2>
        <p className="muted">
          A lookup spends <strong>your</strong> credit, so there is no shared
          key by design and nothing but a button press can start one. Saving a
          key does not call the provider; verifying does, and that costs one
          lookup.
        </p>
        <Loading {...keys} what="your provider keys" />
        {keys.data ? (
          <>
            <ul className="list">
              {providers.map((row) => (
                <li key={row.provider} className="list-row">
                  <div>
                    <strong>{row.provider}</strong>{" "}
                    <span className="badge">
                      {row.connected
                        ? row.usable
                          ? "connected"
                          : "needs attention"
                        : "no key"}
                    </span>
                    {row.label ? <div className="hint">{row.label}</div> : null}
                  </div>
                  {mayAdminister && row.connected ? (
                    <div className="btn-row">
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          postJson(`/provider-keys/${row.provider}/verify`, {
                            json: {},
                          })
                            .then((body) =>
                              onToast(
                                body.ok
                                  ? body.note
                                  : `Not working: ${body.reason}`,
                              ),
                            )
                            .catch(onError)
                        }
                      >
                        Verify
                      </button>
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          deleteJson(`/provider-keys/${row.provider}`)
                            .then(() => {
                              onToast("Key revoked.");
                              keys.reload();
                            })
                            .catch(onError)
                        }
                      >
                        Revoke
                      </button>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
            <p className="hint">
              {keys.data.used_today} of {keys.data.daily_lookup_budget} lookups
              used today. That limit is ours, not the provider&apos;s: it exists
              so a loop cannot spend your credits.
            </p>
          </>
        ) : null}

        {mayAdminister ? (
          <form onSubmit={save} style={{ marginTop: 14 }}>
            <Field label="Provider">
              <select
                value={provider || providers[0]?.provider || ""}
                onChange={(event) => setProvider(event.target.value)}
              >
                {providers.map((row) => (
                  <option key={row.provider} value={row.provider}>
                    {row.provider}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="API key">
              <input
                type="password"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
              />
            </Field>
            <Field label="Client id" hint="Snov only.">
              <input
                value={clientId}
                onChange={(event) => setClientId(event.target.value)}
              />
            </Field>
            <button
              type="submit"
              className="btn"
              disabled={busy || !apiKey || !(provider || providers[0])}
            >
              Save key
            </button>
          </form>
        ) : null}
      </section>

      <section className="card">
        <h2>Look somebody up</h2>
        {usable.length === 0 ? (
          <p className="muted">
            Add one of your own provider keys first. There is no shared key, by
            design.
          </p>
        ) : (
          <form onSubmit={lookup}>
            <Field label="Provider">
              <select
                value={lookupWith || usable[0].provider}
                onChange={(event) => setLookupWith(event.target.value)}
              >
                {usable.map((row) => (
                  <option key={row.provider} value={row.provider}>
                    {row.provider}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Company domain"
              hint="The company's own domain. A job board's host is not a company, and a lookup there would spend a credit on the wrong people."
            >
              <input
                value={domain}
                onChange={(event) => setDomain(event.target.value)}
                placeholder="acme.example"
              />
            </Field>
            <button
              type="submit"
              className="btn"
              data-variant="primary"
              disabled={busy || !domain}
            >
              Look up — spends one credit
            </button>
          </form>
        )}
      </section>

      <section className="card">
        <h2>Proposed contacts</h2>
        <p className="muted">
          A proposal is not a lead: nothing here can be emailed until you import
          it, and importing goes through the ordinary suppression check. A{" "}
          <strong>guess</strong> is an address built from a naming pattern
          rather than a verified one.
        </p>
        <Loading {...candidates} what="the proposed contacts" />
        {candidates.data ? (
          candidates.data.contact_candidates.length === 0 ? (
            <Empty icon={Icons.inbox} title="Nothing proposed yet">
              Run a lookup above.
            </Empty>
          ) : (
            <ul className="list">
              {candidates.data.contact_candidates.map((row) => (
                <li key={row.id} className="list-row">
                  <div>
                    <strong>{row.full_name || "No name given"}</strong>{" "}
                    <span className="badge">{row.email_status}</span>
                    <span className="badge">{row.state}</span>
                    <div className="hint">
                      {row.title || "No title"} —{" "}
                      {row.email || "no address returned"} ({row.company_domain}
                      , via {row.provider})
                    </div>
                  </div>
                  {row.state === "proposed" ? (
                    <div className="btn-row">
                      {/* Importing is what turns a proposal into something that can be
                          emailed, and it goes through the ordinary importer -- so the
                          suppression check, the MX check and the already-in-this-campaign
                          check all apply. Without a campaign there is nowhere to put it. */}
                      {campaign && row.email ? (
                        <button
                          type="button"
                          className="btn"
                          data-variant="primary"
                          onClick={() =>
                            postJson(`/contact-candidates/${row.id}/import`, {
                              json: { campaign_id: campaign.id },
                            })
                              .then(() => {
                                onToast(`Imported into ${campaign.name}.`);
                                candidates.reload();
                              })
                              .catch(onError)
                          }
                        >
                          Import as a lead
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          postJson(`/contact-candidates/${row.id}/dismiss`, {
                            json: {},
                          })
                            .then(() => {
                              onToast(
                                "Dismissed. It will not be proposed again.",
                              );
                              candidates.reload();
                            })
                            .catch(onError)
                        }
                      >
                        Dismiss
                      </button>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          )
        ) : null}
      </section>
    </>
  );
}

export default function JobHunt({ campaign, onError, onToast, mayAdminister }) {
  const [tab, setTab] = useState("postings");

  return (
    <>
      <h1 className="page-title">Job hunt</h1>
      <p className="page-note">
        The candidate half of this system: postings, the facts you are willing
        to claim, a tailored resume built only from those facts, your own
        network, and hiring contacts on your own provider key.{" "}
        <strong>Nothing on this screen contacts anybody.</strong>
      </p>

      <div className="tabs" role="tablist">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className="tab"
            data-active={tab === item.id ? "true" : undefined}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "postings" ? (
        <Postings
          onError={onError}
          onToast={onToast}
          mayAdminister={mayAdminister}
        />
      ) : tab === "profile" ? (
        <Profile onError={onError} onToast={onToast} />
      ) : tab === "resume" ? (
        <ResumeAndApplications onError={onError} onToast={onToast} />
      ) : tab === "network" ? (
        <Network onError={onError} onToast={onToast} />
      ) : (
        <Contacts
          campaign={campaign}
          onError={onError}
          onToast={onToast}
          mayAdminister={mayAdminister}
        />
      )}
    </>
  );
}
