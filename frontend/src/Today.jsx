import { Empty, PageHeader, Skeleton, useLoad } from "./ui";
import { getJson, postJson } from "./api";

export default function Today() {
  const [data, load] = useLoad(() => getJson("/opportunities/today"));

  async function markDone(task) {
    await postJson(`/tasks/${task.id}`, { json: { task_status: "done" } });
    load.reload();
  }

  const tasks = data?.tasks ?? [];
  const pending = tasks.filter((t) => t.status === "pending");

  return (
    <div style={{ padding: "0 16px" }}>
      <PageHeader title="Today" />
      {load.loading ? (
        <Skeleton rows={3} />
      ) : pending.length === 0 ? (
        <Empty title="Nothing due today">Tasks from your opportunities appear here when they are due.</Empty>
      ) : (
        <div>
          {pending.map((t) => (
            <div
              key={t.id}
              className="card"
              style={{ display: "flex", gap: 12, alignItems: "flex-start", marginBottom: 8 }}
            >
              <input
                type="checkbox"
                style={{ marginTop: 2 }}
                onChange={() => markDone(t)}
              />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 14, fontWeight: 500 }}>{t.title}</div>
                {t.due_at && (
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                    Due {new Date(t.due_at).toLocaleDateString()}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
