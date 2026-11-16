// Importing a sheet of leads, and undoing an import that went wrong.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.

import { useState } from "react";

import { deleteJson, uploadFile } from "./api";
import { Icons, PageHeader, verdictTone } from "./ui";

export default function Import({ campaignId, onError, onToast }) {
  const [preview, setPreview] = useState(null);
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  //: The last import, kept so it can be undone. Only in this component's state on purpose: an
  //: undo offered for ever would eventually be pressed long after the leads were worked on, and
  //: the API refuses to remove anything already emailed regardless.
  const [lastImport, setLastImport] = useState(null);
  const [dragging, setDragging] = useState(false);

  // One path for a file however it arrives: picked, or dropped.
  const loadPreview = async (chosen) => {
    if (!chosen) return;
    setFile(chosen);
    setBusy(true);
    try {
      setPreview(
        await uploadFile(`/campaigns/${campaignId}/imports/preview`, chosen),
      );
    } catch (err) {
      setPreview(null);
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const choose = (event) => {
    const chosen = event.target.files?.[0];
    // Cleared so that picking the *same* file again fires a change event. Without it, a
    // cancelled import cannot be restarted with the file it was cancelled on.
    event.target.value = "";
    loadPreview(chosen);
  };

  // Dropping a sheet on the page is how anybody who has used a spreadsheet expects to start.
  // The file input stays: a drop target on its own is unreachable from a keyboard.
  const onDrop = (event) => {
    event.preventDefault();
    setDragging(false);
    loadPreview(event.dataTransfer?.files?.[0]);
  };

  const commit = async () => {
    setBusy(true);
    try {
      const result = await uploadFile(
        `/campaigns/${campaignId}/imports/commit`,
        file,
      );
      onToast(
        `Imported ${result.imported} lead(s). Research is queued.`,
        "success",
      );
      setLastImport({
        batchId: result.batch_id,
        imported: result.imported,
        name: file.name,
      });
      setPreview(null);
      setFile(null);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const cancel = () => {
    setPreview(null);
    setFile(null);
    // The file input keeps its value after a cancel, so choosing the *same* file again would
    // fire no change event and look like the button was broken.
    const input = document.getElementById("sheet");
    if (input) input.value = "";
  };

  const undo = async () => {
    if (!lastImport?.batchId) return;
    setBusy(true);
    try {
      const result = await deleteJson(
        `/campaigns/${campaignId}/imports/${lastImport.batchId}`,
      );
      onToast(result.message, result.removed ? "success" : undefined);
      setLastImport(null);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeader
        icon={Icons.import}
        eyebrow="Workspace"
        title={<>Import leads</>}
      >
        The preview writes nothing. Committing re-reads the file and re-checks
        every row against the suppression list, so anyone who opted out between
        the two is still excluded.
      </PageHeader>

      <div className="card">
        {/* The drop target is a label wrapping the real input, so clicking it opens the picker
            and a keyboard reaches it through the input's own focus — a div with an onDrop is
            invisible to anybody not using a mouse. */}
        <label
          className="dropzone"
          htmlFor="sheet"
          data-over={dragging}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <span className="empty-icon">
            <Icons.import size={20} />
          </span>
          <strong>
            {dragging ? "Drop it here" : "Drop a sheet, or choose a file"}
          </strong>
          <span className="muted">
            .csv or .xlsx, up to 5 MB and 5,000 rows
          </span>
          <input
            id="sheet"
            type="file"
            accept=".csv,.xlsx"
            onChange={choose}
            disabled={busy}
            style={{
              position: "absolute",
              width: 1,
              height: 1,
              opacity: 0,
              pointerEvents: "none",
            }}
          />
        </label>
        <p className="hint">
          Needs an email column. Name, company and website columns are used for
          research and for the draft; a missing website means the draft has less
          to stand on.
        </p>
      </div>

      {lastImport && !preview ? (
        <div className="banner" data-tone="info">
          <span>
            Imported {lastImport.imported} lead(s) from{" "}
            <strong>{lastImport.name}</strong>. Wrong sheet?{" "}
            <button
              type="button"
              className="btn"
              onClick={undo}
              disabled={busy}
              style={{ marginLeft: 6 }}
            >
              Undo this import
            </button>
            <span className="muted" style={{ display: "block", marginTop: 6 }}>
              Removes the leads this upload created, along with their research
              and drafts. Nothing is added to the suppression list — nobody here
              asked not to be contacted — so the corrected sheet imports
              normally. A lead that has already been emailed is kept, and you
              will be told how many.
            </span>
          </span>
        </div>
      ) : null}

      {preview ? (
        <div className="card">
          <div className="card-head">
            {Object.entries(preview.counts).map(([verdict, count]) => (
              <span
                key={verdict}
                className="badge"
                data-tone={verdictTone(verdict)}
              >
                {count} {verdict}
              </span>
            ))}
            {preview.truncated ? (
              <span className="badge" data-tone="warn">
                {preview.truncated} rows beyond the cap were dropped
              </span>
            ) : null}
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Row</th>
                  <th>Email</th>
                  <th>Verdict</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {preview.rows.slice(0, 50).map((row) => (
                  <tr key={row.row_number}>
                    <td className="num">{row.row_number}</td>
                    <td className="mono">{row.email || "—"}</td>
                    <td>
                      <span
                        className="badge"
                        data-tone={verdictTone(row.verdict)}
                      >
                        {row.verdict}
                      </span>
                    </td>
                    <td className="muted">{row.reasons.join("; ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="btn-row">
            <button
              type="button"
              className="btn"
              data-variant="primary"
              onClick={commit}
              disabled={busy || !preview.can_commit}
            >
              Import the importable rows
            </button>
            <button
              type="button"
              className="btn"
              onClick={cancel}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
          <p className="muted" style={{ marginTop: 8 }}>
            Nothing has been written yet. Cancelling forgets this file entirely.
          </p>
          {!preview.can_commit ? (
            <p className="muted" style={{ marginTop: 10 }}>
              Nothing here can be imported. Check that the sheet has email, name
              and company columns.
            </p>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
