// The sending controls built in Phases 11 and 14, none of which had a screen.
//
// F4's deliverability report, F7's mailbox pool, F15's booking webhook and F17's search keys were
// all reachable only with curl. Three of them are the kind of thing that is useless unless
// somebody can see it:
//
//   * **The booking webhook** needs a URL and a secret pasted into a third party's settings page.
//     An API-only feature here means the webhook is never wired up, and the only symptom is a
//     funnel that reports no meetings.
//   * **The deliverability report** answers "will my mail arrive?", which is the question behind
//     every other number on the dashboard.
//   * **The mailbox pool** decides which address a campaign sends from, and a pool nobody can see
//     is a campaign sending from an address its operator did not choose.

import { useCallback, useEffect, useState } from "react";

import { deleteJson, getJson, postJson, putJson } from "./api";
import { useLoad } from "./useLoad";
import { Empty, Icons, SkeletonList } from "./ui";

const TABS = [
  { id: "pool", label: "Sending pool" },
  { id: "deliverability", label: "Deliverability" },
  { id: "booking", label: "Booking webhook" },
  { id: "search", label: "Search keys" },
];

const VERDICT_LABELS = {
  pass: "pass",
  fail: "fail",
  warn: "warning",
  unknown: "unknown",
};

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

function Loading({ failed, data, what }) {
  if (failed) return <p className="muted">Could not load {what}.</p>;
  if (!data) return <SkeletonList count={2} rows={2} />;
  return null;
}

function Pool({ campaign, onError, onToast, mayAdminister }) {
  const pool = useLoad(`/campaigns/${campaign.id}/mailboxes`, onError);
  const all = useLoad("/mailboxes", onError);
  const [chosen, setChosen] = useState("");
  const [weight, setWeight] = useState(1);
  const [busy, setBusy] = useState(false);

  const members = pool.data?.mailboxes || [];
  const memberIds = new Set(members.map((row) => row.mailbox_id));
  const addable = (all.data?.mailboxes || []).filter(
    (row) => !memberIds.has(row.id),
  );

  const add = (event) => {
    event.preventDefault();
    const id = chosen || addable[0]?.id;
    if (!id) return;
    setBusy(true);
    putJson(`/campaigns/${campaign.id}/mailboxes/${id}`, {
      json: { weight: Number(weight) },
    })
      .then(() => {
        onToast("Added to the pool.");
        pool.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  return (
    <>
      <section className="card">
        <h2>Sending pool</h2>
        <p className="muted">{pool.data?.note || ""}</p>
        <Loading {...pool} what="the sending pool" />
        {pool.data ? (
          <ul className="list">
            {members.map((row) => (
              <li key={row.mailbox_id} className="list-row">
                <div>
                  <strong>{row.from_email}</strong>{" "}
                  {row.primary ? <span className="badge">primary</span> : null}
                  {row.paused ? <span className="badge">paused</span> : null}
                  {!row.usable && !row.paused ? (
                    <span className="badge">cannot send</span>
                  ) : null}
                  <div className="hint">Share: {row.weight}</div>
                </div>
                {mayAdminister ? (
                  <div className="btn-row">
                    {/* Pausing stops this mailbox sending anywhere, not just in this campaign.
                        It is the manual half of ADR-034, which pauses one by itself when its
                        bounce rate crosses the limit -- and resuming is deliberately a
                        person's decision, because that is the risky direction. */}
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        postJson(
                          `/mailbox/${row.mailbox_id}/${row.paused ? "resume" : "pause"}`,
                          { json: {} },
                        )
                          .then(() => {
                            onToast(
                              row.paused
                                ? "Sending resumed."
                                : "Sending paused.",
                            );
                            pool.reload();
                          })
                          .catch(onError)
                      }
                    >
                      {row.paused ? "Resume sending" : "Pause sending"}
                    </button>
                    {/* The primary's membership comes from `campaigns.mailbox_id`, so its
                        share and its removal are not this screen's to change. */}
                    <input
                      type="number"
                      min={1}
                      max={100}
                      defaultValue={row.weight}
                      disabled={row.primary}
                      style={{ width: 70 }}
                      onBlur={(event) => {
                        const next = Number(event.target.value);
                        if (next === row.weight) return;
                        putJson(
                          `/campaigns/${campaign.id}/mailboxes/${row.mailbox_id}`,
                          {
                            json: { weight: next },
                          },
                        )
                          .then(() => {
                            onToast("Share changed.");
                            pool.reload();
                          })
                          .catch(onError);
                      }}
                    />
                    <button
                      type="button"
                      className="btn"
                      disabled={row.primary}
                      onClick={() =>
                        deleteJson(
                          `/campaigns/${campaign.id}/mailboxes/${row.mailbox_id}`,
                        )
                          .then((body) => {
                            // Leads already on this mailbox stay on it: reassigning them would
                            // do the exact thing the sticky sender exists to prevent, to the
                            // people furthest along in a conversation.
                            onToast(body?.note || "Removed from the pool.");
                            pool.reload();
                          })
                          .catch(onError)
                      }
                    >
                      Remove
                    </button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}

        {mayAdminister && addable.length ? (
          <form onSubmit={add} style={{ marginTop: 14 }}>
            <Field label="Add a mailbox">
              <select
                value={chosen || addable[0].id}
                onChange={(e) => setChosen(e.target.value)}
              >
                {addable.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.from_email}
                    {row.usable ? "" : " (cannot send yet)"}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Share"
              hint="Relative to the other mailboxes, not a daily limit. Each mailbox keeps its own cap: a pool shares the work out, it does not raise anybody's limit."
            >
              <input
                type="number"
                min={1}
                max={100}
                value={weight}
                onChange={(event) => setWeight(event.target.value)}
              />
            </Field>
            <button type="submit" className="btn" disabled={busy}>
              Add to pool
            </button>
          </form>
        ) : null}
      </section>
    </>
  );
}

function Deliverability({ onError }) {
  const report = useLoad("/admin/sender-requirements", onError);

  return (
    <section className="card">
      <h2>Deliverability</h2>
      <p className="muted">
        One report per sending <strong>domain</strong>, because SPF, DKIM and
        DMARC belong to a domain rather than to an address. A check that could
        not be answered is reported as unknown rather than as a failure: a DNS
        timeout is not evidence of absence.
      </p>
      <Loading {...report} what="the deliverability report" />
      {report.data ? (
        report.data.domains.length === 0 ? (
          <Empty icon={Icons.inbox} title="No sending domains yet">
            Connect a mailbox and its domain appears here.
          </Empty>
        ) : (
          report.data.domains.map((domain) => (
            <div key={domain.domain} style={{ marginTop: 14 }}>
              <h3>
                {domain.domain}{" "}
                <span className="badge">
                  {domain.ready ? "ready" : "not ready"}
                </span>
                {domain.provider_owned ? (
                  <span className="badge">provider&apos;s domain</span>
                ) : null}
              </h3>
              {domain.provider_owned ? (
                <p className="hint">
                  Your provider handles authentication for this domain, so there
                  is nothing in DNS for you to publish. You do share its
                  reputation with everybody else sending from it, and you cannot
                  add it to Postmaster Tools because it is not yours — sending
                  from a domain you own gives you both.
                </p>
              ) : null}
              <ul className="list">
                {domain.checks.map((check) => (
                  <li key={check.name} className="list-row">
                    <div>
                      <strong>{check.name}</strong>{" "}
                      <span className="badge">
                        {VERDICT_LABELS[check.verdict] || check.verdict}
                      </span>
                      <div className="hint">{check.detail}</div>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ))
        )
      ) : null}
      {report.data ? (
        <p className="hint" style={{ marginTop: 12 }}>
          A published DKIM record is not proof that anything is signing with it.
          Send one message to a Gmail address and check{" "}
          <strong>Show original</strong> for <code>DKIM: PASS</code>; no DNS
          lookup can answer that.
        </p>
      ) : null}
    </section>
  );
}

function Booking({ onError, onToast, mayAdminister }) {
  const hook = useLoad("/booking-webhook", onError);
  const bookings = useLoad("/bookings", onError);
  // Shown exactly once, held in state and never stored: a secret in localStorage is a secret in
  // a backup of somebody's browser profile.
  const [secret, setSecret] = useState(null);
  const [busy, setBusy] = useState(false);

  const generate = () => {
    setBusy(true);
    postJson("/booking-webhook/secret", { json: {} })
      .then((body) => {
        setSecret(body);
        hook.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  return (
    <>
      <section className="card">
        <h2>Booking webhook</h2>
        <p className="muted">{hook.data?.note || ""}</p>
        <Loading {...hook} what="the booking webhook" />
        {hook.data ? (
          <>
            {!hook.data.public_base_set ? (
              <p className="muted">
                <strong>PUBLIC_API_URL is not set on the server</strong>, so
                there is no public address to give your booking tool. Set it and
                this screen will show the URL.
              </p>
            ) : null}
            {hook.data.configured ? (
              <>
                <Field
                  label="Webhook URL"
                  hint="Paste this into your booking tool."
                >
                  <input
                    readOnly
                    value={hook.data.url}
                    onFocus={(e) => e.target.select()}
                  />
                </Field>
                <p className="hint">
                  {hook.data.last_delivery_at
                    ? `Last delivery ${new Date(hook.data.last_delivery_at).toLocaleString()}.`
                    : "Nothing has ever been delivered to this URL. If you have taken a booking since wiring it up, the URL is probably in the wrong field."}
                </p>
                <p className="hint">
                  Sign each delivery with HMAC-SHA256 over the raw request body,
                  in one of: {(hook.data.signature_headers || []).join(", ")}.
                </p>
              </>
            ) : (
              <p className="muted">
                No secret yet, so this workspace receives no bookings. Nothing
                is switched on by being migrated — an admin has to generate one.
              </p>
            )}
            {mayAdminister ? (
              <div className="btn-row">
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  onClick={generate}
                >
                  {hook.data.configured
                    ? "Replace the secret"
                    : "Generate a secret"}
                </button>
                {hook.data.configured ? (
                  <button
                    type="button"
                    className="btn"
                    data-variant="danger"
                    onClick={() =>
                      deleteJson("/booking-webhook/secret")
                        .then(() => {
                          setSecret(null);
                          onToast(
                            "Webhook turned off. Bookings already recorded are untouched.",
                          );
                          hook.reload();
                        })
                        .catch(onError)
                    }
                  >
                    Turn it off
                  </button>
                ) : null}
              </div>
            ) : null}
          </>
        ) : null}

        {secret ? (
          <div className="card" style={{ marginTop: 14 }}>
            <h3>Your signing secret</h3>
            <p className="muted">{secret.note}</p>
            <Field label="Secret">
              <input
                readOnly
                value={secret.secret}
                onFocus={(e) => e.target.select()}
              />
            </Field>
            <Field label="URL">
              <input
                readOnly
                value={secret.url}
                onFocus={(e) => e.target.select()}
              />
            </Field>
            <button
              type="button"
              className="btn"
              onClick={() => setSecret(null)}
            >
              I have copied it
            </button>
          </div>
        ) : null}
      </section>

      <section className="card">
        <h2>Bookings</h2>
        <p className="muted">
          Including the ones that matched nobody, which are the point of this
          list: usually the person replied from a second address or forwarded
          the link, and dropping them would leave the funnel quietly one meeting
          short.
        </p>
        <Loading {...bookings} what="the bookings" />
        {bookings.data ? (
          bookings.data.bookings.length === 0 ? (
            <Empty icon={Icons.inbox} title="No bookings recorded">
              A verified delivery that we could not read is the one case worth
              looking for in the worker log; everything else answers 204 and
              does nothing.
            </Empty>
          ) : (
            <>
              <p className="hint">
                {bookings.data.unmatched} of {bookings.data.bookings.length}{" "}
                matched nobody.
              </p>
              <ul className="list">
                {bookings.data.bookings.map((row) => (
                  <li key={row.id} className="list-row">
                    <div>
                      <strong>
                        {row.invitee_name ||
                          row.invitee_email ||
                          "Unknown invitee"}
                      </strong>{" "}
                      <span className="badge">{row.kind}</span>
                      <span className="badge">{row.provider}</span>
                      {row.lead_id ? null : (
                        <span className="badge">matched nobody</span>
                      )}
                      <div className="hint">
                        {row.scheduled_at
                          ? `For ${new Date(row.scheduled_at).toLocaleString()}`
                          : "No start time given"}
                        {row.matched_by
                          ? ` — matched by ${row.matched_by}`
                          : ""}
                      </div>
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

function SearchKeys({ onError, onToast, mayAdminister }) {
  const keys = useLoad("/search-keys", onError);
  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);

  const providers = keys.data?.providers || [];

  const save = (event) => {
    event.preventDefault();
    const name = provider || providers[0]?.provider;
    if (!name) return;
    setBusy(true);
    putJson(`/search-keys/${name}`, { json: { api_key: apiKey } })
      .then(() => {
        setApiKey("");
        onToast("Key saved. It is never shown again.");
        keys.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  return (
    <>
      <section className="card">
        <h2>Search keys</h2>
        <p className="muted">{keys.data?.note || ""}</p>
        <Loading {...keys} what="your search keys" />
        {keys.data ? (
          <>
            <h3>The order a search would actually run in</h3>
            {keys.data.order.length === 0 ? (
              <p className="muted">
                No search provider configured, so research uses a company&apos;s
                own pages only — which is a supported setup, not a fault.
              </p>
            ) : (
              <ol className="list">
                {keys.data.order.map((row) => (
                  <li key={row.provider} className="list-row">
                    <div>
                      <strong>{row.provider}</strong>{" "}
                      {row.shared ? (
                        <span className="badge">shared free tier</span>
                      ) : null}
                      <div className="hint">
                        {row.used_this_month} of{" "}
                        {keys.data.monthly_budget_per_provider} used this month
                        {row.shared
                          ? " — this is the deployment’s own account, shared by every workspace using it. Add your own key to go ahead of it."
                          : ""}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            )}

            <h3 style={{ marginTop: 14 }}>Providers</h3>
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
                  </div>
                  {mayAdminister && row.connected ? (
                    <div className="btn-row">
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          postJson(`/search-keys/${row.provider}/verify`, {
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
                        Verify — spends a credit
                      </button>
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          deleteJson(`/search-keys/${row.provider}`)
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
          </>
        ) : null}

        {mayAdminister && providers.length ? (
          <form onSubmit={save} style={{ marginTop: 14 }}>
            <Field label="Provider">
              <select
                value={provider || providers[0].provider}
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
            <button type="submit" className="btn" disabled={busy || !apiKey}>
              Save key
            </button>
          </form>
        ) : null}
      </section>
    </>
  );
}

export default function Sending({ campaign, onError, onToast, mayAdminister }) {
  const [tab, setTab] = useState("pool");

  return (
    <>
      <h1 className="page-title">Sending</h1>
      <p className="page-note">
        Which addresses this campaign sends from, whether their domains meet the
        bulk-sender rules, where a booked meeting comes in from, and which
        search provider pays for research.
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

      {tab === "pool" ? (
        <Pool
          campaign={campaign}
          onError={onError}
          onToast={onToast}
          mayAdminister={mayAdminister}
        />
      ) : tab === "deliverability" ? (
        <Deliverability onError={onError} />
      ) : tab === "booking" ? (
        <Booking
          onError={onError}
          onToast={onToast}
          mayAdminister={mayAdminister}
        />
      ) : (
        <SearchKeys
          onError={onError}
          onToast={onToast}
          mayAdminister={mayAdminister}
        />
      )}
    </>
  );
}
