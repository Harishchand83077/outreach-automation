// A router, in about a hundred lines and with no dependency (S17.1, ADR-057).
//
// **Why hand-written.** This project has twelve hand-drawn SVG icons instead of an icon package
// for a stated reason: a dependency is a tree to audit for the life of the project, and the
// alternative here is sixty lines. What a router has to do in this app is parse a path, match a
// pattern, push history and re-render -- all of which the platform already does. The things
// `react-router` adds beyond that, which are real, are nested outlets, loaders, lazy route
// splitting and transitions. None of them is in use on a nine-page dashboard.
//
// **Why real paths and not hashes.** `frontend/wrangler.jsonc` already sets
// `not_found_handling: "single-page-application"`, so Cloudflare serves `index.html` for an
// unknown path and the router gets its chance. That was configured before any router existed --
// the comment in that file says so -- which is the only reason History API routing is safe here.
// On a host without that rewrite a refresh on `/leads/123` would 404 and hashes would be the
// right answer instead.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

const RouteContext = createContext({ path: "/", params: {} });

/** Push a path and tell the app. `replace` for a redirect nobody should be able to go back to. */
export function navigate(to, { replace = false } = {}) {
  const next = to.startsWith("/") ? to : `/${to}`;
  if (next === window.location.pathname + window.location.search) return;
  if (replace) window.history.replaceState({}, "", next);
  else window.history.pushState({}, "", next);
  // `pushState` fires no event, by design, so the one listener below needs telling. A custom
  // event rather than a module-level subscriber list: the browser already has a bus.
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/**
 * Match a path against a pattern with `:name` segments.
 *
 * Returns the params, or `null` for no match. A trailing `/*` matches anything after it, which
 * is what lets one screen own its own sub-paths without this file knowing about them.
 */
export function match(pattern, path) {
  const wanted = pattern.split("/").filter(Boolean);
  const given = path.split("/").filter(Boolean);
  const params = {};

  for (let index = 0; index < wanted.length; index += 1) {
    const part = wanted[index];
    if (part === "*") return params;
    const value = given[index];
    if (value === undefined) return null;
    if (part.startsWith(":")) {
      params[part.slice(1)] = decodeURIComponent(value);
      continue;
    }
    if (part !== value) return null;
  }
  return wanted.length === given.length ? params : null;
}

export function RouterProvider({ children }) {
  const [path, setPath] = useState(() => window.location.pathname || "/");

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname || "/");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Scroll to the top on a navigation, because a long page followed by a short one otherwise
  // opens scrolled past its own heading -- which reads as a broken page rather than a scrolled
  // one. Not on the first render, so a deep link to an anchor still works.
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [path]);

  const value = useMemo(() => ({ path, params: {} }), [path]);
  return (
    <RouteContext.Provider value={value}>{children}</RouteContext.Provider>
  );
}

export function useRoute() {
  return useContext(RouteContext);
}

/**
 * The first pattern in `routes` that matches, with its params.
 *
 * Order matters and that is deliberate: `/campaigns/new` has to be listed before
 * `/campaigns/:id`, or "new" becomes an id and the screen asks the API for a campaign called
 * "new". Stated here because it is the one way this is easy to get wrong.
 */
export function useMatch(routes) {
  const { path } = useRoute();
  return useMemo(() => {
    for (const route of routes) {
      const params = match(route.path, path);
      if (params) return { route, params };
    }
    return null;
  }, [routes, path]);
}

/** An anchor that routes instead of reloading. A real `href`, so middle-click and copy work. */
export function Link({ to, children, className, onClick, ...rest }) {
  const { path } = useRoute();
  const active = path === to || (to !== "/" && path.startsWith(`${to}/`));

  const handle = (event) => {
    // Let the browser have the ones the browser is better at: a new tab, a new window, a
    // download. Intercepting those is the single most annoying thing a client-side router does.
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
      return;
    event.preventDefault();
    onClick?.(event);
    navigate(to);
  };

  return (
    <a
      href={to}
      className={className}
      onClick={handle}
      aria-current={active ? "page" : undefined}
      {...rest}
    >
      {children}
    </a>
  );
}

/** `useState` that lives in the query string, so a filtered list is a link somebody can send. */
export function useQueryParam(name, fallback = "") {
  const { path } = useRoute();
  const value =
    new URLSearchParams(window.location.search).get(name) ?? fallback;

  const set = useCallback(
    (next) => {
      const params = new URLSearchParams(window.location.search);
      if (next === "" || next === null || next === undefined)
        params.delete(name);
      else params.set(name, next);
      const query = params.toString();
      // `replace`, so typing in a filter box does not put twenty entries in the back stack and
      // make the back button useless for leaving the page.
      navigate(`${window.location.pathname}${query ? `?${query}` : ""}`, {
        replace: true,
      });
    },
    [name],
  );

  // `path` is in the dependency list so a navigation re-reads the query string.
  return useMemo(() => [value, set], [value, set, path]);
}
