/**
 * The side panel: connect, read this page's questions, show an answer per question.
 *
 * **Every suggestion is rendered as text the person copies.** There is no "fill the form"
 * button, and the code that would write one does not exist -- the panel never messages the page
 * back, and `collect.js` has no listener to receive it if it did. A test over this package's
 * source asserts that no form-writing or submitting API appears anywhere in it.
 *
 * The three things the panel is careful about:
 *
 * - **A question with no approved answer is shown as needing the user**, blank, never filled with
 *   a guess. That is the whole difference from the tools this is not copying.
 * - **A partial scan says so.** A board that renders inside a closed shadow root yields fewer
 *   questions than the page has, and a short list with no warning reads as a complete one.
 * - **Text from the page is set with `textContent`**, never as HTML. A question is text somebody
 *   else wrote, and this panel is an extension page with the extension's own privileges.
 */

import { ApiError, Client, type RecentJob, type Suggestion } from "./api";
import * as config from "./config";
import type { Collected } from "./form";
import type { JobInfo } from "./jobinfo";
import type { PersonInfo } from "./personinfo";

const el = <T extends HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`panel.html has no #${id}`);
  return node as T;
};

const ui = {
  status: el("status"),
  setup: el("setup"),
  work: el("work"),
  api: el<HTMLInputElement>("api"),
  token: el<HTMLInputElement>("token"),
  connect: el<HTMLButtonElement>("connect"),
  forget: el<HTMLButtonElement>("forget"),
  suggest: el<HTMLButtonElement>("suggest"),
  saveJob: el<HTMLButtonElement>("save-job"),
  savePerson: el<HTMLButtonElement>("save-person"),
  personPreview: el("person-preview"),
  personName: el("person-name"),
  personHeadline: el("person-headline"),
  personUrl: el("person-url"),
  personConfirm: el<HTMLButtonElement>("person-confirm"),
  personCancel: el<HTMLButtonElement>("person-cancel"),
  jobPreview: el("job-preview"),
  jobTitle: el("job-title"),
  jobUrl: el("job-url"),
  jobConfirm: el<HTMLButtonElement>("job-confirm"),
  jobCancel: el<HTMLButtonElement>("job-cancel"),
  jobDone: el("job-done"),
  jobNote: el("job-note"),
  jobSize: el("job-size"),
  jobConfirmText: el<HTMLButtonElement>("job-confirm-text"),
  siteAccess: el("site-access"),
  recent: el<HTMLUListElement>("recent"),
  grantSites: el<HTMLButtonElement>("grant-sites"),
  readAs: el("read-as"),
  partial: el("partial"),
  list: el<HTMLOListElement>("suggestions"),
  error: el("error"),
};

let client: Client | null = null;

function fail(message: string): void {
  ui.error.textContent = message;
  ui.error.hidden = false;
}

function clearError(): void {
  ui.error.textContent = "";
  ui.error.hidden = true;
}

function recentRow(job: RecentJob): HTMLLIElement {
  const item = document.createElement("li");
  const title = document.createElement("div");
  // `textContent`: a title is text from somebody else's page.
  title.textContent = job.company_domain ? `${job.title} (${job.company_domain})` : job.title;
  item.append(title);
  const meta = document.createElement("div");
  meta.className = "muted";
  const saved = job.saved_at ? job.saved_at.slice(0, 10) : "";
  meta.textContent = job.fit
    ? `Saved ${saved}. Fit ${Math.round(job.fit.score * 100)}%: ${job.fit.matched} of ${job.fit.requirements} requirements matched your confirmed facts.`
    : `Saved ${saved}. Not scored yet; score it in the dashboard.`;
  item.append(meta);
  return item;
}

async function loadRecent(): Promise<void> {
  if (!client) return;
  try {
    const { jobs } = await client.recentJobs();
    if (!jobs.length) {
      const empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "Nothing saved yet.";
      ui.recent.replaceChildren(empty);
      return;
    }
    ui.recent.replaceChildren(...jobs.map(recentRow));
  } catch {
    // A list that fails to load is not worth an error banner; the buttons above still work.
  }
}

async function connect(apiUrl: string, token: string): Promise<void> {
  const next = new Client(apiUrl, token);
  const who = await next.whoami();
  client = next;
  ui.status.textContent = `Connected as ${who.role}.`;
  ui.work.hidden = false;
  void loadRecent();
  // The setup form stays reachable -- a token lapses and the panel must not become a dead end.
  ui.setup.hidden = false;
}

ui.connect.addEventListener("click", async () => {
  clearError();
  ui.connect.disabled = true;
  try {
    const apiUrl = ui.api.value.trim();
    const token = ui.token.value.trim();
    if (!config.isAcceptableApiUrl(apiUrl)) {
      fail("The API address must be an https:// URL.");
      return;
    }
    if (!token) {
      fail("Paste the extension token from Settings in the dashboard.");
      return;
    }
    if (!(await config.grantApiAccess(apiUrl))) {
      fail("Chrome needs permission to reach that address before the token can be checked.");
      return;
    }
    await connect(apiUrl, token);
    await config.save({ apiUrl, token });
  } catch (error) {
    ui.work.hidden = true;
    client = null;
    fail(error instanceof ApiError ? error.message : "Could not reach that address.");
  } finally {
    ui.connect.disabled = false;
  }
});

ui.forget.addEventListener("click", async () => {
  await config.clear();
  client = null;
  ui.token.value = "";
  ui.work.hidden = true;
  ui.list.replaceChildren();
  ui.status.textContent = "Not connected.";
  clearError();
});

/**
 * Read the page the user is looking at.
 *
 * `activeTab` is what makes this legitimate and it is also what makes it fail politely: it does
 * not cover `chrome://` pages, the Web Store, or a PDF viewer, and `executeScript` rejects on
 * those rather than returning nothing. The refusal is reported as the page being unreadable,
 * because from the user's side that is what happened.
 */
async function injectInto<T>(file: string): Promise<T> {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("No page is open in this window.");
  let injected;
  try {
    [injected] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: [file],
    });
  } catch (error) {
    // `activeTab` covers only the tab the toolbar icon was clicked on; a side panel outlives
    // that. Offer the optional host grant, which the person gives on a click and can withdraw.
    if (/permission|Cannot access/i.test(String(error))) {
      ui.siteAccess.hidden = false;
      throw new Error("Chrome needs your permission to read this page. Press Allow on websites, then try again.");
    }
    throw error;
  }
  const result = injected?.result as T | undefined;
  if (!result) throw new Error("This page cannot be read. Chrome blocks extensions on it.");
  return result;
}

const readActivePage = (): Promise<Collected> => injectInto<Collected>("collect.js");

let pendingJob: JobInfo | null = null;
/** What the person last pressed, so granting site access can carry on where they were. */
let lastRun: (() => Promise<void>) | null = null;

function closeJobPreview(): void {
  pendingJob = null;
  ui.jobPreview.hidden = true;
}

/** Two presses on purpose: the first reads and shows, the second is the only one that sends. */
async function runSaveJob(): Promise<void> {
  lastRun = runSaveJob;
  clearError();
  ui.jobDone.hidden = true;
  if (!client) {
    fail("Connect first.");
    return;
  }
  try {
    const job = await injectInto<JobInfo>("job.js");
    if (!/^https?:/i.test(job.url) || !job.title) {
      fail("This page does not look like a job posting I can read a title from.");
      return;
    }
    const known = await client.lookupJob(job.url).catch(() => ({ saved: false }));
    ui.jobNote.textContent = known.saved
      ? "You already saved this posting. Saving again only refreshes it."
      : "";
    pendingJob = job;
    ui.jobSize.textContent = job.text
      ? `"Save with posting text" also sends ${job.text.length.toLocaleString()} characters of this page's text, so it can be scored against your confirmed facts and read by your connected Claude. Form fields are never read.`
      : "";
    ui.jobConfirmText.hidden = !job.text;
    ui.jobTitle.textContent = job.company_domain ? `${job.title} (${job.company_domain})` : job.title;
    ui.jobUrl.textContent = job.url;
    ui.jobPreview.hidden = false;
  } catch (error) {
    fail(error instanceof Error ? error.message : "Something went wrong reading this page.");
  }
}

ui.saveJob.addEventListener("click", () => void runSaveJob());

let pendingPerson: PersonInfo | null = null;

function closePersonPreview(): void {
  pendingPerson = null;
  ui.personPreview.hidden = true;
}

/** Same two presses as a job: read and show, then send only on the second. */
async function runSavePerson(): Promise<void> {
  lastRun = runSavePerson;
  clearError();
  ui.jobDone.hidden = true;
  if (!client) {
    fail("Connect first.");
    return;
  }
  try {
    const person = await injectInto<PersonInfo>("person.js");
    if (!/^https?:/i.test(person.url) || !person.full_name) {
      fail("I could not read a person's name from this page.");
      return;
    }
    pendingPerson = person;
    ui.personName.textContent = person.full_name;
    ui.personHeadline.textContent = [person.headline, person.company_name].filter(Boolean).join(" / ");
    ui.personUrl.textContent = person.url;
    ui.personPreview.hidden = false;
  } catch (error) {
    fail(error instanceof Error ? error.message : "Something went wrong reading this page.");
  }
}

ui.savePerson.addEventListener("click", () => void runSavePerson());
ui.personCancel.addEventListener("click", closePersonPreview);
ui.personConfirm.addEventListener("click", async () => {
  if (!client || !pendingPerson) return;
  ui.personConfirm.disabled = true;
  try {
    const saved = await client.savePerson(pendingPerson);
    ui.jobDone.textContent = saved.created
      ? "Saved. Find them under People to look up in the dashboard."
      : "You had already saved this person.";
    ui.jobDone.hidden = false;
    closePersonPreview();
  } catch (error) {
    fail(error instanceof Error ? error.message : "Could not save this person.");
  } finally {
    ui.personConfirm.disabled = false;
  }
});

ui.grantSites.addEventListener("click", async () => {
  const granted = await chrome.permissions.request({ origins: ["https://*/*"] });
  ui.siteAccess.hidden = granted;
  if (granted) {
    clearError();
    // Carry on with what they pressed rather than making them press it twice.
    await lastRun?.();
  }
});

ui.jobCancel.addEventListener("click", closeJobPreview);

async function confirmJob(withText: boolean): Promise<void> {
  if (!client || !pendingJob) return;
  ui.jobConfirm.disabled = true;
  ui.jobConfirmText.disabled = true;
  try {
    const { text, ...rest } = pendingJob;
    const saved = await client.captureJob(withText ? { ...rest, body_text: text } : rest);
    ui.jobDone.textContent = saved.created
      ? "Saved. Find it under Jobs in the dashboard."
      : "You had already saved this posting; it is refreshed.";
    ui.jobDone.hidden = false;
    closeJobPreview();
    void loadRecent();
  } catch (error) {
    fail(error instanceof Error ? error.message : "Could not save this job.");
  } finally {
    ui.jobConfirm.disabled = false;
    ui.jobConfirmText.disabled = false;
  }
}

ui.jobConfirm.addEventListener("click", () => void confirmJob(false));
ui.jobConfirmText.addEventListener("click", () => void confirmJob(true));

function row(suggestion: Suggestion): HTMLLIElement {
  const item = document.createElement("li");
  if (suggestion.state === "needs_user") item.classList.add("needs-user");

  const question = document.createElement("div");
  question.className = "question";
  // `textContent`, because this string came off somebody else's page.
  question.textContent = suggestion.field_key;
  item.append(question);

  const box = document.createElement("textarea");
  box.value = suggestion.answer ?? "";
  box.placeholder =
    suggestion.state === "needs_user"
      ? "No approved answer for this question yet. Write one here, and add it to your answer bank in the dashboard."
      : "";
  box.setAttribute("aria-label", suggestion.field_key);
  item.append(box);

  const actions = document.createElement("div");
  actions.className = "row";

  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = "Copy";
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(box.value);
    copy.textContent = "Copied";
    setTimeout(() => {
      copy.textContent = "Copy";
    }, 2000);
    // Recorded as accepted or edited depending on whether they changed it: an edit is the most
    // valuable of the three outcomes, because it says what the answer should have been.
    const changed = box.value.trim() !== (suggestion.answer ?? "").trim();
    await client?.outcome(suggestion.id, changed ? "edited" : "accepted").catch(() => undefined);
  });

  const reject = document.createElement("button");
  reject.type = "button";
  reject.className = "secondary";
  reject.textContent = "Not useful";
  reject.addEventListener("click", async () => {
    reject.disabled = true;
    await client?.outcome(suggestion.id, "rejected").catch(() => undefined);
  });

  actions.append(copy, reject);

  // Only where there was no approved answer, or the person rewrote it: the useful case is
  // "I just wrote this for the first time", and the bank only ever receives it unapproved.
  const remember = document.createElement("button");
  remember.type = "button";
  remember.className = "secondary";
  remember.textContent = "Remember for later";
  remember.addEventListener("click", async () => {
    const text = box.value.trim();
    if (!text) {
      remember.textContent = "Write an answer first";
      return;
    }
    remember.disabled = true;
    try {
      const result = await client?.proposeAnswer(suggestion.field_key, text);
      remember.textContent = result?.saved ? "Sent for your approval" : "Already in your bank";
    } catch {
      remember.disabled = false;
      remember.textContent = "Could not save; try again";
    }
  });
  if (suggestion.state === "needs_user") actions.append(remember);
  item.append(actions);

  if (suggestion.fact_ids.length) {
    const facts = document.createElement("p");
    facts.className = "facts";
    facts.textContent = `From your confirmed facts: ${suggestion.fact_ids.join(", ")}`;
    item.append(facts);
  }
  return item;
}

async function runSuggest(): Promise<void> {
  lastRun = runSuggest;
  clearError();
  if (!client) {
    fail("Connect first.");
    return;
  }
  ui.suggest.disabled = true;
  ui.list.replaceChildren();
  try {
    const page = await readActivePage();
    ui.readAs.textContent = `Read as a ${page.via} form: ${page.fields.length} question(s) of ${page.controls} field(s).`;
    ui.partial.hidden = !page.partial;
    ui.partial.textContent = page.partial
      ? "Some fields on this page could not be read, so this list may be incomplete."
      : "";
    if (!page.fields.length) {
      fail("No questions were found on this page.");
      return;
    }
    const response = await client.suggest(
      page.url,
      page.fields.map((field) => field.key),
    );
    ui.list.replaceChildren(...response.suggestions.map(row));
  } catch (error) {
    fail(error instanceof Error ? error.message : "Something went wrong reading this page.");
  } finally {
    ui.suggest.disabled = false;
  }
}

ui.suggest.addEventListener("click", () => void runSuggest());

void (async () => {
  const stored = await config.load();
  ui.api.value = stored.apiUrl;
  if (!stored.apiUrl || !stored.token) return;
  ui.token.value = stored.token;
  try {
    await connect(stored.apiUrl, stored.token);
  } catch {
    ui.status.textContent = "Your saved token was refused. Paste a new one.";
  }
})();
