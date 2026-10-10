// Supabase is used for **authentication only** (ADR-001). No table is ever read through this
// client: every piece of data comes from our own API, which verifies the token and resolves it to
// a `members` row before answering. That is what keeps tenancy in one place.
//
// The anon key is public by design — it identifies the project, it does not authorise anything —
// so it belongs in the Cloudflare build variables rather than in this repo.

import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL || ''
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY || ''

export const authConfigured = Boolean(url && anonKey)

// Built even when unconfigured, so the app can render a page explaining what is missing rather
// than throwing at import time and showing a blank screen.
export const supabase = authConfigured
  ? createClient(url, anonKey, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    })
  : null

export async function accessToken() {
  if (!supabase) return null
  const { data } = await supabase.auth.getSession()
  return data?.session?.access_token || null
}

export async function signOut() {
  if (supabase) await supabase.auth.signOut()
}
