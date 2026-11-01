/**
 * The client, the stored configuration, and the two refusals that matter.
 */

import { describe, expect, it, vi } from "vitest";

import { Client, endpoint, readDetail } from "../src/api";
import { isAcceptableApiUrl } from "../src/config";

describe("where the client sends things", () => {
  it("builds the gateway path and tolerates a trailing slash", () => {
    expect(endpoint("https://api.example.com/", "/whoami")).toBe(
      "https://api.example.com/ext/v1/whoami",
    );
  });

  it("asks only for /ext/v1, never /api/v1 or /mcp", () => {
    // ADR-039's third door from this side: there is no base path here that could reach the
    // dashboard's API or the MCP connector, so a leaked extension token has nothing to open.
    for (const path of ["/whoami", "/suggest", "/suggestions/x/outcome"]) {
      const built = endpoint("https://api.example.com", path);
      expect(built).toContain("/ext/v1/");
      expect(built).not.toContain("/api/v1");
      expect(built).not.toContain("/mcp");
    }
  });

  it("sends the token as a bearer and no cookies", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ connected: true, role: "owner", scopes: ["ext:suggest"] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await new Client("https://api.example.com", "oa_secret").whoami();

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Authorization"]).toBe("Bearer oa_secret");
    // Omitted deliberately: this endpoint authenticates by token alone, and sending a browser's
    // credentials would make the extension a confused deputy for any session on that host.
    expect(init.credentials).toBe("omit");
    vi.unstubAllGlobals();
  });

  it("shows the server's own sentence rather than a status code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: "An extension token is required." }), {
          status: 401,
        }),
      ),
    );

    await expect(new Client("https://api.example.com", "nope").whoami()).rejects.toThrow(
      "An extension token is required.",
    );
    vi.unstubAllGlobals();
  });

  it("reads a detail out of a body that is not JSON without throwing", () => {
    expect(readDetail("<html>502</html>")).toBe("<html>502</html>");
    expect(readDetail("")).toBeNull();
  });
});

describe("the API address", () => {
  it("must be https, because the token is a bearer credential", () => {
    expect(isAcceptableApiUrl("https://api.example.com")).toBe(true);
    expect(isAcceptableApiUrl("http://api.example.com")).toBe(false);
    expect(isAcceptableApiUrl("not a url")).toBe(false);
    expect(isAcceptableApiUrl("")).toBe(false);
  });

  it("allows plain http on localhost only, for whoever is building the server", () => {
    expect(isAcceptableApiUrl("http://localhost:8000")).toBe(true);
    expect(isAcceptableApiUrl("http://127.0.0.1:8000")).toBe(true);
    // Not a localhost-looking name on somebody else's host.
    expect(isAcceptableApiUrl("http://localhost.evil.test")).toBe(false);
  });
});
