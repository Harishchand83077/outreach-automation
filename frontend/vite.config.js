import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Which build of the dashboard this is (S17.3). Cloudflare names the variable differently
// between its Pages and Workers builders and may rename it again, so several candidates are
// tried and an unknown build is left unknown rather than guessed: the comparison against the
// API's build is only sound when both sides actually know theirs.
const env = process.env
const UI_COMMIT =
  env.WORKERS_CI_COMMIT_SHA ||
  env.CF_PAGES_COMMIT_SHA ||
  env.GITHUB_SHA ||
  env.BUILD_COMMIT ||
  ''

export default defineConfig({
  plugins: [react()],
  define: {
    // Short, because this is only ever compared with a short sha from `git rev-parse --short`
    // and shown to a person. Comparing a long sha with a short one is a mismatch that is not
    // one, which would be the worst possible behaviour for a banner about mismatches.
    __UI_COMMIT__: JSON.stringify(UI_COMMIT.slice(0, 7)),
    __UI_BUILT_AT__: JSON.stringify(new Date().toISOString()),
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
