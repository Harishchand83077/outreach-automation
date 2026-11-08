// One `useLoad`, because there were four (S17.1).
//
// `JobHunt.jsx`, `Sending.jsx`, `Signals.jsx` and `Operations.jsx` each defined their own copy of
// the same eighteen lines, and the Overview page was about to be the fifth. They had already
// started to differ -- some returned `failed`, some did not, and none of them could tell
// "loading" from "loaded and empty", which is the distinction that twice made this dashboard
// state something false while it waited.
//
// **`asked` is the field that copy did not have, and it is the one that matters.** `data !== null`
// means "we have something to show". `asked` means "we have heard back, including hearing an
// error" -- and only the second one justifies a sentence like "no campaigns yet". The old
// campaigns list started as `[]`, so for the half-second before the fetch returned the screen
// said "No campaign yet" to somebody with two of them.
//
// Both dashboard guards already know the name `useLoad`: `test_dashboard_calls_real_routes.py`
// lists it in `_PATH_TAKERS` and `test_dashboard_reads_real_fields.py` matches
// `const x = useLoad('/path')`. Keeping the name is what keeps those two guards pointed at this
// code -- the first version of the route guard matched only `api.js` helpers and was therefore
// blind to the two screens it had been written for.

import { useCallback, useEffect, useState } from "react";

import { getJson } from "./api";

/**
 * Load one path, and say which of the three states we are in.
 *
 * Returns `{ data, failed, asked, reload }`:
 * - `data` -- the body, or null.
 * - `failed` -- the request came back and it was an error.
 * - `asked` -- the request came back at all. `!asked` is the only honest "loading".
 * - `reload` -- ask again. Stable, so it is safe in a dependency list.
 *
 * `deps` re-runs the load when something other than the path changes.
 *
 * `onError` is called with the error so one place in the app decides what an error means: an
 * expired session is not a red box, it is a reason to go back to the sign-in screen.
 */
export function useLoad(path, onError, deps = []) {
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);
  const [asked, setAsked] = useState(false);

  const load = useCallback(() => {
    setFailed(false);
    getJson(path)
      .then(setData)
      .catch((err) => {
        setFailed(true);
        onError?.(err);
      })
      .finally(() => setAsked(true));
    // `deps` is spread so a caller can re-fetch when something other than the path changes --
    // the job-hunt screen reloads its lists after a write. The lint rule cannot see through a
    // spread, which is the one place in this app it is disabled rather than satisfied.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, onError, ...deps]);

  useEffect(load, [load]);

  return { data, failed, asked, reload: load };
}
