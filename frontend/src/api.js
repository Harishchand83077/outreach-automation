// One place where every API call goes through, so a non-JSON response produces a sentence a
// person can act on instead of a parser error.
//
// The failure this exists for: when the dashboard is served by Cloudflare and no backend origin
// is configured, a request to /api/leads is answered by the static-asset handler with
// index.html and a **200**. `if (!r.ok)` therefore passes, `r.json()` is handed `<!DOCTYPE html>`
// and React shows "Unexpected token '<', "<!DOCTYPE "... is not valid JSON" — which says nothing
// about the actual problem, that the API is not reachable.

const _base = import.meta.env.VITE_API_URL || ''
export const API = _base ? _base.replace(/\/api\/?$/, '') + '/api' : '/api'

// True when the page was loaded from somewhere that cannot also be serving the API.
const SAME_ORIGIN = !_base

export class ApiUnreachable extends Error {
  constructor(path) {
    super(
      SAME_ORIGIN
        ? `The API did not answer ${path}. This build has no VITE_API_URL set, so it is asking ` +
          `the site's own origin for /api, and getting the dashboard's HTML back. Set ` +
          `VITE_API_URL to the backend's address and rebuild.`
        : `The API at ${_base} did not answer ${path} with JSON. Check that the backend is ` +
          `running and that VITE_API_URL points at it.`
    )
    this.name = 'ApiUnreachable'
  }
}

async function parse(response, path) {
  // An HTML body means we reached a web server but not the API, whatever the status says.
  const type = response.headers.get('content-type') || ''
  if (!type.includes('json')) throw new ApiUnreachable(path)
  return response.json()
}

// GET returning parsed JSON. Throws ApiUnreachable for an HTML answer, Error for an API error.
export async function getJson(path) {
  let response
  try {
    response = await fetch(`${API}${path}`)
  } catch {
    // A network-level failure: nothing answered at all.
    throw new ApiUnreachable(path)
  }
  if (!response.ok) {
    const body = await parse(response, path).catch(() => null)
    throw new Error((body && body.detail) || response.statusText || `HTTP ${response.status}`)
  }
  return parse(response, path)
}

// POST, with an optional body. Same contract as getJson.
export async function postJson(path, { body, json } = {}) {
  const init = { method: 'POST' }
  if (json !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(json)
  } else if (body !== undefined) {
    init.body = body
  }

  let response
  try {
    response = await fetch(`${API}${path}`, init)
  } catch {
    throw new ApiUnreachable(path)
  }
  if (!response.ok) {
    const parsed = await parse(response, path).catch(() => null)
    throw new Error((parsed && parsed.detail) || response.statusText || `HTTP ${response.status}`)
  }
  return parse(response, path)
}
