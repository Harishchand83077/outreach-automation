/**
 * Where the API is and what the token is, in `chrome.storage.local`.
 *
 * **`local`, never `sync`.** `chrome.storage.sync` would push the token through the user's Google
 * account to every browser they sign into, which is a credential copied to machines nobody
 * decided to put it on. A token pasted on one laptop stays on that laptop.
 *
 * **The API must be https.** The token is a bearer credential; over http it is readable by
 * anything on the path. A localhost exception exists for whoever is developing the server, and
 * is the one case where plain http is not a downgrade.
 */

export interface Config {
  apiUrl: string;
  token: string;
}

const KEY = "outreach.config";

export function isAcceptableApiUrl(raw: string): boolean {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return false;
  }
  if (url.protocol === "https:") return true;
  return url.protocol === "http:" && (url.hostname === "localhost" || url.hostname === "127.0.0.1");
}

export async function load(): Promise<Config> {
  const stored = await chrome.storage.local.get(KEY);
  const value = stored[KEY] as Partial<Config> | undefined;
  return { apiUrl: value?.apiUrl ?? "", token: value?.token ?? "" };
}

export async function save(config: Config): Promise<void> {
  if (!isAcceptableApiUrl(config.apiUrl)) {
    throw new Error("The API address must be an https:// URL.");
  }
  await chrome.storage.local.set({ [KEY]: config });
}

export async function clear(): Promise<void> {
  await chrome.storage.local.remove(KEY);
}

/**
 * Ask for access to the API host, and to that host alone.
 *
 * The manifest ships with **no** `host_permissions`: the extension cannot reach any site until
 * the user saves their own API address, at which point Chrome asks them about that one origin.
 * A wildcard host permission in the package would be a prompt covering the entire web, granted
 * once at install time for a reason nobody could evaluate.
 */
export async function grantApiAccess(apiUrl: string): Promise<boolean> {
  const origin = `${new URL(apiUrl).origin}/*`;
  if (await chrome.permissions.contains({ origins: [origin] })) return true;
  return chrome.permissions.request({ origins: [origin] });
}
