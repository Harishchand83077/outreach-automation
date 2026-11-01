/**
 * The only server this package talks to: `/ext/v1`, with an `ext:suggest` token (ADR-039).
 *
 * **That token cannot call `/api/v1` or `/mcp`.** It is the third door, and the reason it exists
 * is this file: a credential that sits in a browser extension on somebody's laptop must not be
 * able to read the workspace's campaigns, leads and replies if it leaks. The server asserts the
 * matrix; this side simply has no other base path to ask for.
 */

export interface Suggestion {
  id: string;
  field_key: string;
  answer: string | null;
  fact_ids: string[];
  state: "proposed" | "needs_user" | "accepted" | "edited" | "rejected";
}

export interface SuggestResponse {
  capture_id: string;
  suggestions: Suggestion[];
  note: string;
}

export interface WhoAmI {
  connected: boolean;
  role: string;
  scopes: string[];
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Trailing slashes removed and the path appended, so a pasted URL with one still works. */
export function endpoint(base: string, path: string): string {
  return `${base.trim().replace(/\/+$/, "")}/ext/v1${path}`;
}

export class Client {
  constructor(
    private readonly base: string,
    private readonly token: string,
  ) {}

  private async call<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(endpoint(this.base, path), {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Authorization: `Bearer ${this.token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      // No cookies: this endpoint authenticates by token and nothing else, and sending a
      // browser's credentials to it would make the extension a confused deputy for any session
      // the user happens to hold on that host.
      credentials: "omit",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      // The server answers every refusal with one sentence; show it rather than a status code,
      // and never retry a 401 -- a wrong token does not become right on a second attempt.
      const detail = await response.text().catch(() => "");
      throw new ApiError(readDetail(detail) ?? `The server answered ${response.status}.`, response.status);
    }
    return (await response.json()) as T;
  }

  whoami(): Promise<WhoAmI> {
    return this.call<WhoAmI>("/whoami");
  }

  suggest(pageUrl: string, labels: string[]): Promise<SuggestResponse> {
    return this.call<SuggestResponse>("/suggest", { page_url: pageUrl, labels });
  }

  outcome(id: string, state: "accepted" | "edited" | "rejected"): Promise<{ recorded: boolean }> {
    return this.call<{ recorded: boolean }>(`/suggestions/${encodeURIComponent(id)}/outcome`, {
      state,
    });
  }
}

/** FastAPI puts the sentence in `detail`; anything else is shown as-is, trimmed. */
export function readDetail(text: string): string | null {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
    }
  } catch {
    /* not JSON; fall through */
  }
  const trimmed = text.trim();
  return trimmed ? trimmed.slice(0, 300) : null;
}
