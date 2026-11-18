// Inviting somebody into a workspace, and taking them out again.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.

import { useCallback, useEffect, useState } from "react";

import { deleteJson, getJson, postJson } from "./api";
import { Field } from "./ui";

export default function Team({ onError, onToast }) {
  const [team, setTeam] = useState(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    // Degrades rather than erroring: an older server without this route should leave the panel
    // out, not put a red banner over a settings screen whose other sections are all fine.
    getJson("/team")
      .then(setTeam)
      .catch(() => setTeam(null));
  }, []);

  useEffect(load, [load]);

  const invite = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const made = await postJson("/team/invitations", {
        json: { email, role },
      });
      setEmail("");
      onToast(
        made.already_invited
          ? `${made.email} was already invited.`
          : `${made.email} can now sign in and will land in this workspace.`,
      );
      load();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id) => {
    try {
      await deleteJson(`/team/invitations/${id}`);
      onToast("Invitation withdrawn.");
      load();
    } catch (err) {
      onError(err);
    }
  };

  const remove = async (id, who) => {
    try {
      await deleteJson(`/team/members/${id}`);
      onToast(
        `${who} no longer has access. Their work stays in this workspace.`,
      );
      load();
    } catch (err) {
      onError(err);
    }
  };

  if (!team) return null;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <p className="muted">
        People here share these campaigns, leads and drafts. Somebody who should
        have their own separate workspace does not belong on this list — they
        sign up with their own email instead.
      </p>
      <ul style={{ listStyle: "none", padding: 0, margin: "12px 0" }}>
        {team.members.map((member) => (
          <li
            key={member.id}
            style={{ display: "flex", alignItems: "center", gap: 8 }}
          >
            <span>{member.email || "no address on file"}</span>
            <span className="badge">{member.role}</span>
            {member.is_you ? <span className="badge">you</span> : null}
            <span className="spacer" />
            {member.is_you ? null : (
              <button
                type="button"
                className="btn"
                data-size="sm"
                data-variant="danger"
                onClick={() => remove(member.id, member.email)}
              >
                Remove
              </button>
            )}
          </li>
        ))}
        {team.invitations.map((row) => (
          <li
            key={row.id}
            style={{ display: "flex", alignItems: "center", gap: 8 }}
          >
            <span className="muted">{row.email}</span>
            <span className="badge">{row.role}</span>
            <span className="badge" data-tone="warn">
              invited
            </span>
            <span className="spacer" />
            <button
              type="button"
              className="btn"
              data-size="sm"
              onClick={() => revoke(row.id)}
            >
              Withdraw
            </button>
          </li>
        ))}
      </ul>
      <form onSubmit={invite} className="field-row">
        <Field
          label="Invite by email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          type="email"
          required
        />
        <label className="field">
          <span>Role</span>
          <select
            value={role}
            onChange={(event) => setRole(event.target.value)}
          >
            <option value="member">member — reviews drafts</option>
            <option value="admin">admin — can also change the rules</option>
            <option value="owner">owner — can also change the send mode</option>
          </select>
        </label>
        <button
          type="submit"
          className="btn"
          data-variant="primary"
          disabled={busy || !email}
        >
          Invite
        </button>
      </form>
      <p className="muted" style={{ marginTop: 8 }}>
        They can be invited before or after they first sign in — but if they
        have already signed in they will have been given a workspace of their
        own, and a person belongs to one workspace here.
      </p>
    </div>
  );
}
