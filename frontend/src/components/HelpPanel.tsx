import { useEffect, useState } from "react";
import { api, Me, UpdateStatus } from "../api";

/** App version, signed desktop updates and the support report. */
export function HelpPanel({ me, onClose }: { me: Me; onClose: () => void }) {
  const [update, setUpdate] = useState<UpdateStatus | null>(null);
  const [link, setLink] = useState("");
  const [showLink, setShowLink] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");

  const refresh = () => api.updateStatus().then(setUpdate).catch(() => setUpdate(null));
  useEffect(() => {
    refresh();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function act(name: string, action: () => Promise<string>) {
    setBusy(name);
    setMessage("");
    try {
      setMessage(await action());
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "That did not work. Please retry.");
    } finally {
      setBusy("");
      refresh();
    }
  }

  async function supportReport() {
    const blob = await api.supportReport();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `LectureTrimmer-support-${new Date().toISOString().slice(0, 10)}.zip`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    return "Support report saved to your Downloads. It has no videos, transcripts or passwords; send it to the tool owner.";
  }

  return (
    <div className="help-backdrop" onClick={onClose}>
      <section className="card help-panel" role="dialog" aria-label="Help and updates" onClick={(e) => e.stopPropagation()}>
        <div className="row-between">
          <h2>Help &amp; updates</h2>
          <button className="ghost icon" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <p className="muted small">
          Lecture Trimmer v{me.app_version} · analysis {me.analysis_version} · speech: {me.speech}
        </p>

        <h3>Updates</h3>
        {update?.installed ? (
          <>
            {update.pending ? (
              <p className="small">Update <strong>v{update.pending}</strong> is downloaded and ready.</p>
            ) : (
              <p className="muted small">Updates are signed by the owner and never touch your saved lectures.</p>
            )}
            <div className="help-actions">
              {update.pending ? (
                <button className="primary small" disabled={!!busy}
                        onClick={() => act("restart", () => api.restartForUpdate().then((r) => r.message))}>
                  {busy === "restart" ? "Restarting…" : "Restart to apply update"}
                </button>
              ) : (
                <button className="primary small" disabled={!!busy}
                        onClick={() => act("check", () => api.checkForUpdate(link.trim()).then((r) => r.message))}>
                  {busy === "check" ? "Checking…" : "Check for updates"}
                </button>
              )}
              <button className="ghost small" onClick={() => setShowLink((v) => !v)}>
                {update.link_saved ? "Change update link" : "Set update link"}
              </button>
            </div>
            {(showLink || !update.link_saved) && (
              <label className="help-link">
                Update link from the owner (latest.json)
                <input value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://drive.google.com/…" spellCheck={false} />
              </label>
            )}
          </>
        ) : (
          <p className="muted small">This copy runs from source code, so it updates with the code itself. Installed desktop copies update here.</p>
        )}

        <h3>Support</h3>
        <p className="muted small">If something goes wrong, save a support report and send it to the tool owner.</p>
        <div className="help-actions">
          <button className="ghost small" disabled={!!busy} onClick={() => act("support", supportReport)}>
            {busy === "support" ? "Saving…" : "Save support report"}
          </button>
        </div>

        {message && <p className="help-message small" role="status">{message}</p>}
      </section>
    </div>
  );
}
