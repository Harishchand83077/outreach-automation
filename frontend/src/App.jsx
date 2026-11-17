// The dashboard, against `/api/v1`.
//
// Rebuilt again for Phase 8. The previous version was one long scroll of every section at once,
// styled with inline objects. Three things forced the change, and each one is a layout decision
// rather than a cosmetic one:
//
// **A reviewer cannot be handed a hundred drafts.** Each carries a subject, a body and its
// research hooks, so the queue pages — a few at a time, with the total still waiting, because
// "47 left" is what tells somebody whether to start now.
//
// **An operator with a thousand leads cannot scroll to find one.** Email is unique so it gets a
// search box; name, company and state get filters, and they combine as AND.
//
// **Everything is scoped to one campaign.** It sits in the bar above the content rather than
// inside each section, because choosing it changes what every screen means.
//
// Settings are behind a role, not merely a hidden button: `member` can review drafts but cannot
// change the rules underneath them, and the API refuses them too.
//
// Plain text throughout. No `dangerouslySetInnerHTML` anywhere — a draft body is a lead's name
// and a model's output, neither of which is trusted (hard rule 7).

import React, { useCallback, useEffect, useMemo, useState } from "react";

import { useSession } from "./Auth";
import ErrorBoundary from "./ErrorBoundary";
import { ApiUnreachable, NotAuthenticated, getJson, getVersion } from "./api";
import { Link, RouterProvider, navigate, useMatch, useRoute } from "./router";
import { signOut } from "./supabase";
import { buildMismatch } from "./build";
import { Mark, PRODUCT } from "./Brand";
import {
  Centered,
  CommandPalette,
  Icons,
  PageHeader,
  SkeletonList,
  ThemeToggle,
  statusTone,
  usePaletteShortcut,
  useTheme,
} from "./ui";
import Admin from "./Admin";
import CampaignScreen from "./Campaign";
import Consent from "./Consent";
import Import from "./Import";
import JobHunt from "./JobHunt";
import Landing from "./Landing";
import Leads from "./Leads";
import NewCampaign from "./NewCampaign";
import Operations from "./Operations";
import Overview from "./Overview";
import Platform from "./Platform";
import ReviewQueue from "./Review";
import Sending from "./Sending";
import Settings from "./Settings";
import Setup from "./Setup";
import Signals from "./Signals";
import "./app.css";
import "./chrome.css";

const NOTHING = () => {};

export default function App() {
  const session = useSession();

  if (session === undefined) return <Centered>Checking your session…</Centered>;

  /* **The consent screen is outside the shell**, and outside the route table with it. A person
     arrives here by redirect from an application asking for access, and every other thing on
     the page -- a rail, a campaign picker, a review queue -- is a thing that could be mistaken
     for the decision. Signed out, they get the front door first and land back here afterwards,
     because the URL does not change while they sign in. */
  const consenting = window.location.pathname === "/oauth/authorize";
  // **The first screen anybody is ever shown.** It used to be the sign-in form alone on a grey
  // page, which is a login screen; this is a front door. The form itself is unchanged -- still a
  // magic link, still no password field anywhere in the bundle.
  if (!session) return <Landing />;
  if (consenting) return <ConsentPage />;
  return (
    <RouterProvider>
      <Dashboard session={session} />
    </RouterProvider>
  );
}

/** The consent screen with the one error rule the rest of the app uses.
 *
 * An expired session here is not a red box: it is a reason to go back to the front door, from
 * which the person returns to this same URL and the application that sent them is none the
 * wiser. */
function ConsentPage() {
  const [error, setError] = useState(null);
  const handle = (err) => {
    if (err instanceof NotAuthenticated) {
      signOut();
      return;
    }
    setError(err?.message || "Something went wrong.");
  };
  return (
    <>
      {error ? (
        <div className="banner" data-tone="bad" role="alert">
          <span>{error}</span>
        </div>
      ) : null}
      <Consent onError={handle} />
    </>
  );
}

/* ------------------------------------------------------------------ the route table

   **Order matters, and this is the one way it is easy to get wrong**: `/campaigns/new` has to
   come before `/campaigns/:id`, or "new" is read as an id and the screen asks the API for a
   campaign by that name. `router.match` returns the first pattern that fits, so the specific
   path goes above the parameterised one.

   `scoped` means the screen needs a campaign. Those keep the campaign picker in the top bar
   rather than nesting under `/campaigns/:id/...`: moving six screens under a campaign's path is
   a change to how every one of them gets its data, and this slice is about the frame. Stated
   here rather than left as a silence, because the nesting is the obvious next step. */

const ROUTES = [
  { path: "/", id: "overview" },
  { path: "/review", id: "review", scoped: true },
  { path: "/leads", id: "leads", scoped: true },
  { path: "/leads/:leadId", id: "leads", scoped: true },
  { path: "/import", id: "import", scoped: true },
  { path: "/campaigns/new", id: "new" },
  { path: "/campaigns/:campaignId", id: "campaign", scoped: true },
  { path: "/signals", id: "signals" },
  { path: "/sending", id: "sending", scoped: true },
  { path: "/job-hunt", id: "jobhunt" },
  { path: "/operations", id: "operations" },
  { path: "/setup", id: "setup" },
  { path: "/team", id: "admin" },
  { path: "/settings", id: "settings", scoped: true },
  { path: "/platform", id: "platform" },
];

function Dashboard({ session }) {
  const [theme, toggleTheme] = useTheme();
  const [paletteOpen, setPaletteOpen] = usePaletteShortcut();
  const [me, setMe] = useState(null);
  const [error, setError] = useState(null);
  const [toasts, setToasts] = useState([]);
  const [campaigns, setCampaigns] = useState([]);
  //: **"Empty" and "not loaded yet" are different answers and must look different.** The list
  //: starts `[]`, which is indistinguishable from "this org has no campaigns" -- so for the
  //: half-second before the fetch returned, the screen said "No campaign yet" to somebody with
  //: two campaigns. A loading state that states a falsehood is worse than a spinner.
  const [campaignsLoaded, setCampaignsLoaded] = useState(false);
  const [campaignId, setCampaignId] = useState(null);
  /* **The route is the state now.** `view` was a `useState`, so every screen was one
     address: no deep link, no back button, nothing to send somebody. `matched` is the first
     route in `ROUTES` whose pattern fits, and `view` is derived from it so the rest of this
     component -- the nav's `aria-current`, the error boundary's reset key -- reads exactly as
     it did. A path nobody recognises falls back to the overview rather than rendering nothing,
     because a blank screen is the one outcome a wrong URL must not produce. */
  const { path } = useRoute();
  const matched = useMatch(ROUTES);
  const view = matched?.route.id ?? "overview";
  const [attention, setAttention] = useState(null);
  const [queueCount, setQueueCount] = useState(null);

  const toast = useCallback((message, kind = "info") => {
    const id = Math.random().toString(36).slice(2);
    setToasts((current) => [...current, { id, message, kind }]);
    setTimeout(
      () => setToasts((current) => current.filter((t) => t.id !== id)),
      6000,
    );
  }, []);

  // One place that decides what an error means. An expired session is not something to show in a
  // red box — it is a reason to send somebody back to the sign-in screen.
  const handle = useCallback((err) => {
    if (err instanceof NotAuthenticated) {
      signOut();
      return;
    }
    setError(
      err instanceof ApiUnreachable ? err.message : String(err.message || err),
    );
  }, []);

  useEffect(() => {
    getJson("/me")
      .then(setMe)
      .catch((err) => {
        if (err instanceof NotAuthenticated) {
          signOut();
          return;
        }
        // 403 means authenticated but not a member: say so plainly rather than showing an
        // empty dashboard that looks broken.
        setError(String(err.message || err));
      });
  }, []);

  const refreshCampaigns = useCallback(() => {
    getJson("/campaigns")
      .then((data) => {
        setCampaigns(data.campaigns);
        setCampaignId((current) => current || data.campaigns[0]?.id || null);
      })
      .catch(handle)
      // Loaded means "we asked and heard back", including hearing an error -- otherwise a
      // failed fetch leaves the screen on a skeleton for ever with the real error in a banner
      // above it.
      .finally(() => setCampaignsLoaded(true));
  }, [handle]);

  const refreshAttention = useCallback(() => {
    getJson("/admin/needs-attention").then(setAttention).catch(handle);
  }, [handle]);

  // The sidebar badge is the count for the *selected* campaign, not the org, because the screen
  // it labels is campaign-scoped — a badge saying 12 above a queue showing 3 is worse than no
  // badge. `limit=1` because only `total` is wanted; the page itself is fetched by the queue.
  const refreshQueueCount = useCallback(() => {
    if (!campaignId) {
      setQueueCount(null);
      return;
    }
    getJson(`/review/drafts?campaign_id=${campaignId}&limit=1`)
      .then((data) => setQueueCount(data.total))
      .catch(handle);
  }, [campaignId, handle]);

  useEffect(() => {
    if (!me) return;
    refreshCampaigns();
    refreshAttention();
  }, [me, refreshCampaigns, refreshAttention]);

  useEffect(() => {
    if (!me) return;
    refreshQueueCount();
  }, [me, refreshQueueCount]);

  /* **A campaign in the URL is the selected campaign.** `/campaigns/:id` is a link somebody
     can send, so opening one has to switch the picker rather than show a different campaign's
     name above this one's data -- which is the mismatch that made Connect Gmail aim at the
     wrong mailbox for three phases. */
  const routeCampaignId = matched?.params.campaignId ?? null;
  useEffect(() => {
    if (routeCampaignId && routeCampaignId !== campaignId) {
      setCampaignId(routeCampaignId);
    }
  }, [routeCampaignId, campaignId]);

  const campaign = useMemo(
    () =>
      campaigns.find((row) => row.id === (routeCampaignId || campaignId)) ||
      null,
    [campaigns, campaignId, routeCampaignId],
  );

  // The gate is the API's; this only decides what to draw. A hidden button is not security, so
  // every endpoint behind these screens checks the role itself.
  const mayAdminister = me ? ["owner", "admin"].includes(me.role) : false;

  // Which build the server is running (S17.3). **Failure is swallowed deliberately**: an API
  // old enough not to serve /version is exactly the situation this exists to explain, and a
  // toast about the check failing would be noise stacked on top of the real symptom.
  const [apiBuild, setApiBuild] = useState(null);
  useEffect(() => {
    let live = true;
    getVersion()
      .then((body) => {
        if (live) setApiBuild(body);
      })
      .catch(NOTHING);
    return () => {
      live = false;
    };
  }, []);
  const mismatch = useMemo(() => buildMismatch(apiBuild), [apiBuild]);
  const isOperator = me ? me.platform_operator === true : false;

  if (error && !me) {
    return (
      <Centered>
        <PageHeader title={<>Cannot load the dashboard</>}>{error}</PageHeader>
        <button type="button" onClick={() => signOut()} className="btn">
          Sign out
        </button>
      </Centered>
    );
  }

  if (!me) {
    return (
      <div className="content" aria-busy="true">
        <div
          className="skeleton"
          style={{ height: 30, width: 220, marginBottom: 18 }}
        />
        <SkeletonList count={2} rows={4} />
      </div>
    );
  }

  /* **Three groups rather than one list of nine.** Nine items in a flat list is a list you
     read word by word; three short runs is one you scan -- and the grouping says something true,
     which is that this product has an outreach half, a job-hunt half and a set of controls.

     Every item carries a `to`, so the command palette gets the paths for free: it is built from
     this same array, which is why a screen added later appears in it without anybody
     remembering. */
  const nav = [
    { group: "Workspace" },
    { id: "overview", label: "Overview", icon: Icons.home, to: "/" },
    {
      id: "review",
      label: "Review queue",
      icon: Icons.review,
      to: "/review",
      count: queueCount ?? undefined,
    },
    { id: "leads", label: "Leads", icon: Icons.leads, to: "/leads" },
    { id: "import", label: "Import", icon: Icons.import, to: "/import" },
    { group: "Outreach" },
    {
      id: "campaign",
      label: "Campaign",
      icon: Icons.campaign,
      to: campaignId ? `/campaigns/${campaignId}` : "/campaigns/new",
    },
    { id: "signals", label: "Signals", icon: Icons.signal, to: "/signals" },
    { id: "sending", label: "Sending", icon: Icons.send, to: "/sending" },
    // The candidate half of the product (F5, F6, F9, F10, F20). Shown to everybody rather than
    // gated on the campaign's kind: the facts, the connections and the answer bank are the
    // workspace's own and are worth filling in before a job-hunt campaign exists.
    { group: "Job hunt" },
    {
      id: "jobhunt",
      label: "Job hunt",
      icon: Icons.briefcase,
      to: "/job-hunt",
    },
    ...(mayAdminister
      ? [
          { group: "Controls" },
          // First under Controls: it is the list of what the other three still need doing.
          { id: "setup", label: "Setup", icon: Icons.check, to: "/setup" },
          { id: "admin", label: "Team", icon: Icons.people, to: "/team" },
          {
            id: "operations",
            label: "Operations",
            icon: Icons.pulse,
            to: "/operations",
          },
          {
            id: "settings",
            label: "Settings",
            icon: Icons.settings,
            to: "/settings",
          },
        ]
      : []),
    // The scope above orgs (ADR-024): granted by hand, held by almost nobody. Drawn from `/me`
    // rather than by calling the route and reading a 403, which would log an error on every
    // load for every ordinary user.
    ...(isOperator
      ? [
          {
            id: "platform",
            label: "Platform",
            icon: Icons.platform,
            to: "/platform",
          },
        ]
      : []),
  ];

  const who = me.email || session.user?.email || "";

  // Every screen and every campaign, two keystrokes away. Built from the same `nav` list the
  // sidebar draws, so a screen added later appears here without anybody remembering to add it.
  const commands = [
    ...nav
      .filter((item) => item.to)
      .map((item) => ({
        id: `view:${item.id}`,
        group: "Go to",
        label: item.label,
        icon: item.icon,
        run: () => navigate(item.to),
      })),
    ...campaigns.map((row) => ({
      id: `campaign:${row.id}`,
      group: "Switch campaign",
      label: row.name,
      icon: Icons.campaign,
      hint: row.paused ? "paused" : row.status,
      run: () => navigate(`/campaigns/${row.id}`),
    })),
    {
      id: "action:new",
      group: "Actions",
      label: "New campaign",
      icon: Icons.plus,
      run: () => navigate("/campaigns/new"),
    },
    {
      id: "action:theme",
      group: "Actions",
      label: "Toggle light and dark",
      icon: Icons.sun,
      run: toggleTheme,
    },
  ];

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Sections">
        <Link to="/" className="brand" style={{ textDecoration: "none" }}>
          <Mark size={30} title={PRODUCT} />
          <span className="brand-name">{PRODUCT}</span>
        </Link>

        {/* An anchor per item, not a button: a real `href` is what makes middle-click, "copy
            link" and the browser's own back button work, and those three are most of what
            "pages instead of buttons" actually means to somebody using it. */}
        {nav.map((item, index) =>
          item.group ? (
            <span key={`group:${item.group}`} className="nav-group-label">
              {item.group}
            </span>
          ) : (
            <Link
              key={item.id}
              to={item.to}
              className="nav-item"
              aria-current={view === item.id ? "page" : undefined}
            >
              <item.icon className="nav-icon" size={17} />
              {item.label}
              {item.count === undefined ? null : (
                <span className="count" data-zero={item.count === 0}>
                  {item.count}
                </span>
              )}
            </Link>
          ),
        )}

        <div className="sidebar-foot">
          <div className="sidebar-user">
            <span className="avatar" aria-hidden="true">
              {who.slice(0, 1) || "?"}
            </span>
            <span className="sidebar-identity">
              <strong title={who}>{who}</strong>
              <span>{me.role}</span>
            </span>
          </div>
          <button
            type="button"
            className="btn"
            data-variant="quiet"
            data-size="sm"
            style={{ width: "100%", marginTop: 6 }}
            onClick={() => signOut()}
          >
            Sign out
          </button>
        </div>
      </nav>

      <div className="main">
        <div className="topbar">
          <label
            className="form-label"
            htmlFor="campaign"
            style={{ margin: 0 }}
          >
            Campaign
          </label>
          <select
            id="campaign"
            value={campaignId || ""}
            onChange={(event) => setCampaignId(event.target.value || null)}
          >
            {campaigns.length === 0 ? (
              <option value="">
                {campaignsLoaded ? "No campaigns yet" : "Loading…"}
              </option>
            ) : null}
            {campaigns.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
          {campaign ? (
            <>
              <span className="badge" data-tone={statusTone(campaign)}>
                {campaign.paused ? "paused" : campaign.status}
              </span>
              {campaign.mailbox_id ? null : (
                <span className="badge" data-tone="warn">
                  no mailbox
                </span>
              )}
            </>
          ) : null}
          {/* Where you are, in the frame rather than in the page. A title alone cannot say it
              when two screens one level apart are both called Settings. */}
          <span className="crumbs">
            <Link to="/">{PRODUCT}</Link>
            {view === "overview" ? null : (
              <>
                <Icons.chevron size={13} />
                <span className="crumbs-current">
                  {nav.find((item) => item.id === view)?.label ??
                    (view === "new" ? "New campaign" : view)}
                </span>
              </>
            )}
          </span>
          <span className="spacer" />
          <button
            type="button"
            className="btn"
            data-size="sm"
            data-variant="quiet"
            onClick={() => setPaletteOpen(true)}
            title="Search (Ctrl K)"
          >
            <Icons.search size={15} />
            <span className="kbd">⌘K</span>
          </button>
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          <Link className="btn" data-variant="brand" to="/campaigns/new">
            <Icons.plus size={15} />
            New campaign
          </Link>
        </div>

        <div className="content">
          {mismatch ? (
            <div className="banner" data-tone="warn" role="status">
              <span>
                {mismatch.apiOlder
                  ? "The server is running an older build than this page."
                  : "The server and this page are different builds."}{" "}
                A screen can ask for something the server does not yet serve, which arrives as
                a plain Not Found. Server {mismatch.api.commit}
                {mismatch.api.built_at ? ` (${mismatch.api.built_at.slice(0, 16)}Z)` : ""}, this
                page {mismatch.ui.commit}
                {mismatch.ui.built_at ? ` (${mismatch.ui.built_at.slice(0, 16)}Z)` : ""}.
              </span>
            </div>
          ) : null}
          {error ? (
            <div className="banner" data-tone="bad" role="alert">
              <span>{error}</span>
              <button
                type="button"
                className="close"
                onClick={() => setError(null)}
              >
                ×
              </button>
            </div>
          ) : null}

          {/* A render error is caught per screen. One bad field name used to blank the
              whole dashboard: no message, no nav, and nothing an operator could report. */}
          <ErrorBoundary resetKey={path} label={view}>
            {view === "overview" ? (
              <Overview onError={handle} mayAdminister={mayAdminister} />
            ) : view === "new" ? (
              <NewCampaign
                onError={handle}
                onToast={toast}
                onCreated={(created) => {
                  refreshCampaigns();
                  navigate(created?.id ? `/campaigns/${created.id}` : "/");
                }}
                onCancel={() => navigate("/")}
              />
            ) : !matched?.route.scoped ? (
              /* A screen that needs no campaign is rendered before the campaign gate below,
                 or Signals, Operations, Team and Platform would all be unreachable in a
                 workspace with no campaign yet -- which is every workspace on its first day. */
              view === "signals" ? (
                <Signals
                  mayAdminister={mayAdminister}
                  onError={handle}
                  onToast={toast}
                />
              ) : view === "jobhunt" ? (
                <JobHunt
                  campaign={campaign}
                  mayAdminister={mayAdminister}
                  onError={handle}
                  onToast={toast}
                />
              ) : view === "operations" ? (
                <Operations onError={handle} onToast={toast} />
              ) : view === "setup" ? (
                <Setup onError={handle} />
              ) : view === "admin" ? (
                <Admin onError={handle} onToast={toast} />
              ) : view === "platform" ? (
                <Platform onError={handle} />
              ) : null
            ) : !campaign && !campaignsLoaded ? (
              <>
                <div
                  className="skeleton"
                  style={{ height: 30, width: 240, marginBottom: 18 }}
                />
                <SkeletonList count={2} rows={3} />
              </>
            ) : !campaign ? (
              <>
                <PageHeader title={<>No campaign yet</>}>
                  A campaign carries the sender persona and the goal, and every
                  other screen is scoped to one. Nothing can be imported,
                  researched or sent until one exists.
                </PageHeader>
                <Link
                  className="btn"
                  data-variant="brand"
                  data-size="lg"
                  to="/campaigns/new"
                >
                  Create a campaign
                </Link>
              </>
            ) : view === "review" ? (
              <ReviewQueue
                campaignId={campaign.id}
                onError={handle}
                onToast={toast}
                onChanged={() => {
                  refreshAttention();
                  refreshQueueCount();
                }}
              />
            ) : view === "leads" ? (
              <Leads
                campaignId={campaign.id}
                /* A lead is a URL now: `/leads/:leadId` opens the drawer on load, so a lead can
                   be linked to, bookmarked and reopened by the back button. The drawer itself
                   stays -- replacing the list was the thing that made comparing two leads mean
                   navigating twice and losing the scroll position both times. */
                openLeadId={matched?.params.leadId ?? null}
                onError={handle}
                onToast={toast}
              />
            ) : view === "import" ? (
              <Import
                campaignId={campaign.id}
                onError={handle}
                onToast={toast}
              />
            ) : view === "campaign" ? (
              <CampaignScreen
                campaign={campaign}
                mayAdminister={mayAdminister}
                onError={handle}
                onToast={toast}
                onChanged={(removed) => {
                  if (removed) {
                    setCampaignId(null);
                    navigate("/");
                  }
                  refreshCampaigns();
                }}
              />
            ) : view === "sending" ? (
              <Sending
                campaign={campaign}
                mayAdminister={mayAdminister}
                onError={handle}
                onToast={toast}
              />
            ) : view === "settings" ? (
              <Settings
                isOwner={me?.role === "owner"}
                campaign={campaign}
                onError={handle}
                onToast={toast}
                onChanged={refreshCampaigns}
              />
            ) : null}
          </ErrorBoundary>
        </div>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        commands={commands}
      />

      <div className="toast-host">
        {toasts.map((item) => (
          <div
            key={item.id}
            className="toast"
            data-kind={item.kind}
            role="status"
          >
            <span className="toast-icon">
              {item.kind === "success" ? (
                <Icons.check size={16} />
              ) : item.kind === "error" ? (
                <Icons.alert size={16} />
              ) : (
                <Icons.info size={16} />
              )}
            </span>
            <span>{item.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ review */
