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

import { ApiError, Client, type Suggestion } from "./api";
import * as config from "./config";
import type { Collected } from "./form";

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

async function connect(apiUrl: string, token: string): Promise<void> {
  const next = new Client(apiUrl, token);
  const who = await next.whoami();
  client = next;
  ui.status.textContent = `Connected as ${who.role}.`;
  ui.work.hidden = false;
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
async function readActivePage(): Promise<Collected> {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("No page is open in this window.");
  const [injected] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["collect.js"],
  });
  const result = injected?.result as Collected | undefined;
  if (!result) throw new Error("This page cannot be read. Chrome blocks extensions on it.");
  return result;
}

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
  item.append(actions);

  if (suggestion.fact_ids.length) {
    const facts = document.createElement("p");
    facts.className = "facts";
    facts.textContent = `From your confirmed facts: ${suggestion.fact_ids.join(", ")}`;
    item.append(facts);
  }
  return item;
}

ui.suggest.addEventListener("click", async () => {
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
});

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
