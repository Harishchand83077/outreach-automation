// People to look up (ADR-084).
//
// Each row is somebody the user saved from a page with the extension's "Save this person" button.
// **Nothing here fetches a profile.** The address is a plain link the user opens themselves in
// their own browser (hard rule 4), finds an email by their own means, and types it in. A person
// only becomes a lead through the ordinary import, which keeps suppression checks and approval.

import { useState } from "react";

import { postJson } from "./api";
import { useLoad } from "./useLoad";
import { Empty, Icons, PageHeader, SkeletonList } from "./ui";

export default function People({ onError, onToast }) {
  const people = useLoad("/people?state=to_look_up", onError);
  const found = useLoad("/people?state=found", onError);
  const [draft, setDraft] = useState({});
  const [busy, setBusy] = useState(null);

  const act = async (id, path, json, done) => {
    setBusy(id);
    try {
      await postJson(`/people/${id}/${path}`, json ? { json } : undefined);
      onToast?.(done);
      people.reload();
      found.reload();
    } catch (error) {
      onError?.(error);
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <PageHeader icon={Icons.leads} eyebrow="Outreach" title={<>People to look up</>}>
        People you saved from the browser extension. Open their page yourself, find a work email,
        and record it here. <strong>Nothing is fetched or emailed from this list</strong>; an
        address becomes a lead only through Import.
      </PageHeader>

      <section className="card">
        <h2>Waiting for an address</h2>
        {!people.asked ? (
          <SkeletonList count={2} rows={2} />
        ) : !people.data?.people?.length ? (
          <Empty icon={Icons.inbox} title="Nobody saved yet">
            Open someone's page in Chrome, then press Save this person in the extension panel.
          </Empty>
        ) : (
          <ul className="list">
            {people.data.people.map((row) => (
              <li key={row.id} className="list-row">
                <div>
                  <strong>{row.full_name}</strong>
                  <div className="muted">
                    {[row.headline, row.company_name].filter(Boolean).join(" / ")}
                  </div>
                  <a href={row.profile_url} target="_blank" rel="noopener noreferrer">
                    Open their page
                  </a>
                </div>
                <div className="row">
                  <input
                    type="email"
                    placeholder="name@company.com"
                    aria-label={`Email for ${row.full_name}`}
                    value={draft[row.id] || ""}
                    onChange={(event) => setDraft({ ...draft, [row.id]: event.target.value })}
                  />
                  <button
                    type="button"
                    disabled={busy === row.id || !(draft[row.id] || "").includes("@")}
                    onClick={() =>
                      act(row.id, "found", { email: draft[row.id] }, "Address recorded.")
                    }
                  >
                    Record address
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy === row.id}
                    onClick={() => act(row.id, "dismiss", null, "Dismissed.")}
                  >
                    Dismiss
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {found.data?.people?.length ? (
        <section className="card">
          <h2>Address found</h2>
          <p className="muted">
            Add these to a campaign through Import. They are not leads yet.
          </p>
          <ul className="list">
            {found.data.people.map((row) => (
              <li key={row.id} className="list-row">
                <strong>{row.full_name}</strong>
                <span className="muted">
                  {row.email} {row.company_name ? ` / ${row.company_name}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  );
}
