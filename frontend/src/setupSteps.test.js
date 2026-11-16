import { describe, expect, it } from "vitest";

import { buildSteps, nextStep, progress } from "./setupSteps";

const heard = (data) => ({ data, asked: true, failed: false });
const waiting = { data: null, asked: false, failed: false };
const errored = { data: null, asked: true, failed: true };

const EMPTY = {
  mailboxes: heard({ mailboxes: [] }),
  sending: heard({ send_mode: "disabled" }),
  contactKeys: heard({ providers: [] }),
  searchKeys: heard({ providers: [] }),
  booking: heard({ configured: false }),
  tokens: heard({ tokens: [] }),
  facts: heard({ facts: [] }),
};

const step = (steps, id) => steps.find((row) => row.id === id);

describe("buildSteps", () => {
  it("never calls a step to-do before its request has come back", () => {
    // The "No campaign yet" defect: a slow request must not read as an instruction.
    const steps = buildSteps({ ...EMPTY, mailboxes: waiting });
    expect(step(steps, "mailbox").state).toBe("unknown");
  });

  it("calls a failed request unknown rather than to-do", () => {
    // A member gets 403 on the admin routes. That is not "you have not set a send mode".
    const steps = buildSteps({ ...EMPTY, sending: errored });
    expect(step(steps, "send-mode").state).toBe("unknown");
  });

  it("is done for a mailbox only when one is usable", () => {
    const usable = buildSteps({
      ...EMPTY,
      mailboxes: heard({ mailboxes: [{ usable: true, needs_reconnect: false }] }),
    });
    expect(step(usable, "mailbox").state).toBe("done");

    const none = buildSteps(EMPTY);
    expect(step(none, "mailbox").state).toBe("todo");
  });

  it("asks for attention when every connection has lapsed", () => {
    const steps = buildSteps({
      ...EMPTY,
      mailboxes: heard({ mailboxes: [{ usable: false, needs_reconnect: true }] }),
    });
    expect(step(steps, "mailbox").state).toBe("attention");
    expect(step(steps, "mailbox").action).toBe("Reconnect");
    expect(step(steps, "mailbox").detail).toMatch(/1 connection has lapsed/);
  });

  it("does not count the fake provider as a connected one", () => {
    const steps = buildSteps({
      ...EMPTY,
      contactKeys: heard({ providers: [{ provider: "mock", usable: true }] }),
    });
    expect(step(steps, "contact-key").state).toBe("todo");
  });

  it("treats a disabled send mode as not yet chosen", () => {
    expect(step(buildSteps(EMPTY), "send-mode").state).toBe("todo");
    const allow = buildSteps({ ...EMPTY, sending: heard({ send_mode: "allowlist" }) });
    expect(step(allow, "send-mode").state).toBe("done");
  });

  it("says when a webhook exists and nothing has ever arrived", () => {
    const steps = buildSteps({
      ...EMPTY,
      booking: heard({ configured: true, last_delivery_at: null }),
    });
    expect(step(steps, "booking").state).toBe("done");
    expect(step(steps, "booking").detail).toMatch(/no booking has arrived/);
  });
});

describe("progress", () => {
  it("counts required steps only", () => {
    const steps = buildSteps({
      ...EMPTY,
      mailboxes: heard({ mailboxes: [{ usable: true }] }),
      tokens: heard({ tokens: [{ id: "1" }] }),
    });
    expect(progress(steps)).toEqual({ done: 1, total: 2, known: true });
  });

  it("is not known while anything is still loading", () => {
    const steps = buildSteps({ ...EMPTY, facts: waiting });
    expect(progress(steps).known).toBe(false);
  });
});

describe("nextStep", () => {
  it("puts a required step ahead of an optional one", () => {
    expect(nextStep(buildSteps(EMPTY)).id).toBe("mailbox");
  });

  it("moves to optional steps once the required ones are done", () => {
    const steps = buildSteps({
      ...EMPTY,
      mailboxes: heard({ mailboxes: [{ usable: true }] }),
      sending: heard({ send_mode: "allowlist" }),
    });
    expect(nextStep(steps).id).toBe("contact-key");
  });

  it("is nothing when everything is done", () => {
    const steps = buildSteps({
      mailboxes: heard({ mailboxes: [{ usable: true }] }),
      sending: heard({ send_mode: "live" }),
      contactKeys: heard({ providers: [{ provider: "hunter", usable: true }] }),
      searchKeys: heard({ providers: [{ connected: true }] }),
      booking: heard({ configured: true, last_delivery_at: "2026-11-01T00:00:00Z" }),
      tokens: heard({ tokens: [{ id: "1" }] }),
      facts: heard({ facts: [{ id: "pp1" }] }),
    });
    expect(nextStep(steps)).toBe(null);
  });
});
