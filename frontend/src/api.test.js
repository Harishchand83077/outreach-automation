// The API layer is where the frontend's only real logic lives, so it is where the tests are.
//
// Three behaviours worth holding, each of which has already gone wrong once:
//
// 1. **An HTML answer is not JSON, whatever the status says.** Cloudflare's SPA fallback returns
//    index.html with a 200 for an unknown path, so `response.ok` is true and `.json()` throws
//    "Unexpected token '<'". That message tells a person nothing about the actual problem.
// 2. **The token is attached in one place.** Twenty call sites each remembering a header is
//    twenty chances for one of them to forget.
// 3. **A 401 is its own case.** It means "sign in again", not "something went wrong" — the UI
//    has to send somebody back to the sign-in screen rather than show them a red box.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./supabase', () => ({
  accessToken: vi.fn(async () => 'a-test-token'),
  signOut: vi.fn(),
  supabase: null,
  authConfigured: true,
}))

const { accessToken } = await import('./supabase')
const { ApiUnreachable, NotAuthenticated, getJson, postJson } = await import('./api')

function respond({ status = 200, json = {}, type = 'application/json', text = '' } = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: 'Status',
    headers: { get: () => type },
    json: async () => json,
    text: async () => text,
  }
}

beforeEach(() => {
  globalThis.fetch = vi.fn()
  accessToken.mockResolvedValue('a-test-token')
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('an HTML answer', () => {
  it('is reported as the API being unreachable, not as a parse error', async () => {
    globalThis.fetch.mockResolvedValue(
      respond({ type: 'text/html', text: '<!DOCTYPE html><html></html>' })
    )
    await expect(getJson('/campaigns')).rejects.toBeInstanceOf(ApiUnreachable)
  })

  it('is caught even when the status is 200', async () => {
    // The whole point: the SPA fallback answers 200, so `response.ok` cannot be trusted alone.
    globalThis.fetch.mockResolvedValue(respond({ status: 200, type: 'text/html' }))
    await expect(getJson('/campaigns')).rejects.toThrow(/did not answer/)
  })

  it('names what to do about it', async () => {
    globalThis.fetch.mockResolvedValue(respond({ type: 'text/html' }))
    await expect(getJson('/campaigns')).rejects.toThrow(/VITE_API_URL/)
  })
})

describe('a network failure', () => {
  it('is the same kind of error as an HTML answer', async () => {
    // "Nothing answered" and "something answered but it was not the API" need the same action.
    globalThis.fetch.mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(getJson('/campaigns')).rejects.toBeInstanceOf(ApiUnreachable)
  })
})

describe('the token', () => {
  it('is attached to every request', async () => {
    globalThis.fetch.mockResolvedValue(respond({ json: { campaigns: [] } }))
    await getJson('/campaigns')

    const [, init] = globalThis.fetch.mock.calls[0]
    expect(init.headers.Authorization).toBe('Bearer a-test-token')
  })

  it('is attached to posts as well as gets', async () => {
    globalThis.fetch.mockResolvedValue(respond({ json: {} }))
    await postJson('/drafts/1/approve', { json: { subject: 'hi' } })

    const [, init] = globalThis.fetch.mock.calls[0]
    expect(init.headers.Authorization).toBe('Bearer a-test-token')
    expect(init.method).toBe('POST')
  })

  it('is simply absent when there is no session, rather than sent empty', async () => {
    accessToken.mockResolvedValue(null)
    globalThis.fetch.mockResolvedValue(respond({ json: {} }))
    await getJson('/campaigns')

    const [, init] = globalThis.fetch.mock.calls[0]
    expect(init.headers.Authorization).toBeUndefined()
  })
})

describe('a 401', () => {
  it('is its own error, so the UI can send the person back to sign in', async () => {
    globalThis.fetch.mockResolvedValue(respond({ status: 401, json: { detail: 'Not authenticated' } }))
    await expect(getJson('/me')).rejects.toBeInstanceOf(NotAuthenticated)
  })

  it('does not leak the server detail to the person', async () => {
    globalThis.fetch.mockResolvedValue(respond({ status: 401, json: { detail: 'token expired' } }))
    await expect(getJson('/me')).rejects.toThrow(/Sign in again/)
  })
})

describe('an API error', () => {
  it('surfaces the detail the API chose to send', async () => {
    globalThis.fetch.mockResolvedValue(
      respond({ status: 409, json: { detail: 'draft is sent, so it cannot be approved' } })
    )
    await expect(postJson('/drafts/1/approve')).rejects.toThrow(/cannot be approved/)
  })

  it('falls back to the status when there is no detail', async () => {
    globalThis.fetch.mockResolvedValue(respond({ status: 500, json: {} }))
    await expect(getJson('/campaigns')).rejects.toThrow(/Status|HTTP 500/)
  })
})

describe('a 204', () => {
  it('returns nothing rather than trying to parse an empty body', async () => {
    globalThis.fetch.mockResolvedValue(respond({ status: 204, type: '' }))
    await expect(getJson('/suppressions/1')).resolves.toBeNull()
  })
})
