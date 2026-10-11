// One place where every API call goes through, so a non-JSON response produces a sentence a
// person can act on instead of a parser error, and so the bearer token is attached in exactly
// one place rather than at twenty call sites.
//
// The failure this exists for: when the dashboard is served by Cloudflare and no backend origin
// is configured, a request to /api/v1/campaigns is answered by the static-asset handler with
// index.html and a **200**. `if (!response.ok)` therefore passes, `response.json()` is handed
// `<!DOCTYPE html>` and React shows "Unexpected token '<', "<!DOCTYPE "... is not valid JSON" —
// which says nothing about the actual problem, that the API is not reachable.

import { accessToken } from './supabase'

const base = import.meta.env.VITE_API_URL || ''
export const API = base ? base.replace(/\/api(\/v1)?\/?$/, '') + '/api/v1' : '/api/v1'

// True when the page was loaded from somewhere that cannot also be serving the API.
const SAME_ORIGIN = !base

export class ApiUnreachable extends Error {
  constructor(path) {
    super(
      SAME_ORIGIN
        ? `The API did not answer ${path}. This build has no VITE_API_URL set, so it is asking ` +
          `the site's own origin for /api/v1, and getting the dashboard's HTML back. Set ` +
          `VITE_API_URL to the backend's address and rebuild.`
        : `The API at ${base} did not answer ${path} with JSON. Check that the backend is ` +
          `running and that VITE_API_URL points at it.`
    )
    this.name = 'ApiUnreachable'
  }
}

export class NotAuthenticated extends Error {
  constructor() {
    super('Your session has expired. Sign in again.')
    this.name = 'NotAuthenticated'
  }
}

async function parse(response, path) {
  // An HTML body means we reached a web server but not the API, whatever the status says.
  const type = response.headers.get('content-type') || ''
  if (!type.includes('json')) throw new ApiUnreachable(path)
  return response.json()
}

async function request(path, init = {}) {
  const token = await accessToken()
  const headers = { ...(init.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`

  let response
  try {
    response = await fetch(`${API}${path}`, { ...init, headers })
  } catch {
    // A network-level failure: nothing answered at all.
    throw new ApiUnreachable(path)
  }

  // 401 is its own case, not a generic error: the UI must send the person back to the sign-in
  // screen rather than showing them a message about a request they cannot retry.
  if (response.status === 401) throw new NotAuthenticated()
  if (response.status === 204) return null

  if (!response.ok) {
    const body = await parse(response, path).catch(() => null)
    const detail = body && body.detail
    // A `detail` can be a string or a structured object — the persona check returns the list of
    // fields that failed. `new Error(object)` would render it "[object Object]" and throw that
    // information away, so the object is carried on the error and only the message is flattened.
    const message =
      typeof detail === 'string'
        ? detail
        : (detail && detail.message) || response.statusText || `HTTP ${response.status}`
    const error = new Error(message)
    if (detail && typeof detail === 'object') error.detail = detail
    error.status = response.status
    throw error
  }
  return parse(response, path)
}

export const getJson = (path) => request(path)

export function postJson(path, { body, json } = {}) {
  const init = { method: 'POST' }
  if (json !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(json)
  } else if (body !== undefined) {
    init.body = body
  }
  return request(path, init)
}

export function patchJson(path, { json } = {}) {
  return request(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(json ?? {}),
  })
}

export const deleteJson = (path) => request(path, { method: 'DELETE' })

// Upload goes through the same path so it gets the same token and the same error handling.
export function uploadFile(path, file) {
  const form = new FormData()
  form.append('file', file)
  return postJson(path, { body: form })
}
