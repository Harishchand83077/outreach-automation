import { Empty, PageHeader, Skeleton, useLoad } from "./ui";
import { getJson } from "./api";

/** Three-pane resume workspace for one opportunity.
 *
 * Props: opportunityId (UUID string), opportunityTitle (display name),
 *        jobPostId (optional UUID, for the fit-score pane).
 */
export default function ResumeWorkspace({ opportunityId, opportunityTitle, jobPostId }) {
  const [versions, versLoad] = useLoad(
    () => getJson(`/resume-versions/for-opportunity/${opportunityId}`),
    [opportunityId],
  );

  const versionList = versions?.versions ?? [];

  if (versLoad.loading) return <Skeleton rows={4} />;
  if (!opportunityId) return <Empty message="Opportunity not found." />;

  return (
    <div style={{ padding: "0 16px" }}>
      <PageHeader title={opportunityTitle ? `Resume — ${opportunityTitle}` : "Resume workspace"} />

      {versionList.length === 0 ? (
        <Empty message="No resume versions for this opportunity yet. Use Job hunt → Resume versions to tailor one." />
      ) : (
        <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
          {/* Left: version list */}
          <div style={{ width: 200, flexShrink: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 8, color: "var(--text-muted)" }}>
              VERSIONS
            </div>
            {versionList.map((v) => (
              <div key={v.id} className="card" style={{ marginBottom: 6, fontSize: 13 }}>
                <div style={{ fontWeight: 500 }}>{v.label}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                  {new Date(v.created_at).toLocaleDateString()}
                </div>
              </div>
            ))}
          </div>

          {/* Centre: rendered text of the latest version */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 8, color: "var(--text-muted)" }}>
              DOCUMENT
            </div>
            <div
              className="card"
              style={{ whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: 12, maxHeight: 480, overflowY: "auto" }}
            >
              {versionList[0]?.rendered_text ?? ""}
            </div>
          </div>

          {/* Right: fit score match if the opportunity has a job post */}
          {jobPostId && (
            <FitPane jobPostId={jobPostId} />
          )}
        </div>
      )}
    </div>
  );
}

function FitPane({ jobPostId }) {
  const [fit, fitLoad] = useLoad(
    () => getJson(`/job-posts/${jobPostId}/fit`),
    [jobPostId],
  );

  if (fitLoad.loading) return <Skeleton rows={4} />;
  if (!fit?.fit) return null;

  const matched = fit.fit.matched ?? [];
  const missing = fit.fit.missing ?? [];

  return (
    <div style={{ width: 220, flexShrink: 0 }}>
      <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 8, color: "var(--text-muted)" }}>
        FIT MATCH
      </div>
      <div style={{ fontSize: 12, marginBottom: 4 }}>
        Score: <strong>{Math.round((fit.fit.score ?? 0) * 100)}%</strong>
      </div>
      {matched.length > 0 && (
        <>
          <div style={{ color: "var(--text-muted)", marginTop: 8, marginBottom: 4, fontSize: 11 }}>MATCHED</div>
          {matched.map((m, i) => (
            <div key={i} style={{ fontSize: 12, marginBottom: 4 }}>
              <span style={{ color: "#2ea043" }}>✓</span> {m.requirement}
            </div>
          ))}
        </>
      )}
      {missing.length > 0 && (
        <>
          <div style={{ color: "var(--text-muted)", marginTop: 8, marginBottom: 4, fontSize: 11 }}>MISSING</div>
          {missing.map((m, i) => (
            <div key={i} style={{ fontSize: 12, marginBottom: 4, color: "var(--text-muted)" }}>
              <span>–</span> {m}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
