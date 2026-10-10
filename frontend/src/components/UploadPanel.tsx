import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, JobOptions, OutputKind, uploadFile } from "../api";
import { bytes, clock, OUTPUT_TITLES } from "../format";

interface Item {
  key: string;
  name: string;
  size: number;
  file?: File; // chosen from this computer
  link?: string; // pasted video link, downloaded by the server; kept only in this page
  context: string;
  state: "waiting" | "uploading" | "ready" | "failed";
  sent: number;
  uploadId?: string;
  duration?: number;
  error?: string;
  controller: AbortController;
}

const PARALLEL_UPLOADS = 2;
const LINK_POLL_MS = 1000;
const DEFAULT_OPTIONS: JobOptions = {
  outputs: ["questions", "theory", "combined"],
  mode: "balanced",
  cut_style: "smooth",
  language: "",
  visual_review: false,
  fresh: false,
  dry_run: false,
};

export function UploadPanel({ videoTypes, onStarted }: { videoTypes: string[]; onStarted: () => void }) {
  const [items, setItems] = useState<Item[]>([]);
  const [options, setOptions] = useState<JobOptions>(DEFAULT_OPTIONS);
  const [allContext, setAllContext] = useState("");
  const [dragging, setDragging] = useState(false);
  const [estimate, setEstimate] = useState<{ inr: number; basis: string } | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [link, setLink] = useState("");
  const [checking, setChecking] = useState(false);
  const [linkError, setLinkError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const started = useRef(new Set<string>());

  const update = (key: string, patch: Partial<Item>) =>
    setItems((list) => list.map((item) => (item.key === key ? { ...item, ...patch } : item)));

  // Keep up to PARALLEL_UPLOADS uploads running; each resumes by itself after a network drop.
  useEffect(() => {
    const running = items.filter((i) => i.file && i.state === "uploading").length;
    const next = items
      .filter((i) => i.file && i.state === "waiting" && !started.current.has(i.key))
      .slice(0, Math.max(0, PARALLEL_UPLOADS - running));
    for (const item of next) {
      started.current.add(item.key);
      update(item.key, { state: "uploading" });
      uploadFile(item.file!, (p) => update(item.key, { sent: p.sent }), item.controller.signal)
        .then((done) => update(item.key, { state: "ready", uploadId: done.upload_id, duration: done.duration }))
        .catch((e) => {
          if (item.controller.signal.aborted) return;
          update(item.key, { state: "failed", error: e instanceof ApiError ? e.message : "Upload failed. Check the connection and retry." });
        });
    }
  }, [items]);

  // The server downloads pasted links; follow its progress until each one is ready.
  const downloading = items.filter((i) => i.link && i.state === "uploading" && i.uploadId);
  const downloadKey = downloading.map((i) => i.key).join();
  useEffect(() => {
    if (!downloadKey) return;
    const timer = setInterval(() => {
      for (const item of downloading) {
        api.uploadStatus(item.uploadId!).then((status) => {
          if (status.complete) update(item.key, { state: "ready", sent: status.size, duration: status.duration ?? undefined });
          else if (status.error) update(item.key, { state: "failed", sent: status.received, error: status.error });
          else update(item.key, { sent: status.received });
        }).catch(() => undefined);
      }
    }, LINK_POLL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloadKey]);

  const ready = items.filter((i) => i.state === "ready");
  const totalSeconds = useMemo(() => ready.reduce((sum, i) => sum + (i.duration || 0), 0), [ready]);
  useEffect(() => {
    if (!totalSeconds) return setEstimate(null);
    api.estimate(totalSeconds).then(setEstimate).catch(() => setEstimate(null));
  }, [totalSeconds]);

  const accepted = (file: File) => videoTypes.some((ext) => file.name.toLowerCase().endsWith(ext));

  function addFiles(files: FileList | File[]) {
    const list = Array.from(files);
    const rejected = list.filter((f) => !accepted(f));
    setError(rejected.length ? `Skipped ${rejected.map((f) => f.name).join(", ")}: not a supported video.` : "");
    setItems((current) => [
      ...current,
      ...list.filter(accepted).map((file) => ({
        key: `${file.name}-${file.size}-${Math.random().toString(36).slice(2)}`,
        name: file.name,
        size: file.size,
        file,
        context: allContext,
        state: "waiting" as const,
        sent: 0,
        controller: new AbortController(),
      })),
    ]);
  }

  async function addLinks() {
    // Several links can be pasted at once, separated by spaces or new lines.
    const urls = link.split(/\s+/).filter(Boolean);
    if (!urls.length) return;
    setChecking(true);
    setLinkError("");
    const failed: string[] = [];
    for (const url of urls) {
      try {
        const added = await api.addLink(url);
        setItems((current) => [...current, {
          key: `link-${added.upload_id}`, name: added.filename, size: added.size, link: url, context: allContext,
          state: "uploading", sent: 0, uploadId: added.upload_id, controller: new AbortController(),
        }]);
      } catch (e) {
        failed.push(url);
        setLinkError(e instanceof Error ? e.message : "This link could not be added.");
      }
    }
    setLink(failed.join(" "));
    setChecking(false);
  }

  function removeItem(item: Item) {
    item.controller.abort();
    if (item.uploadId) api.discardUpload(item.uploadId).catch(() => undefined);
    setItems((list) => list.filter((i) => i.key !== item.key));
  }

  function retryItem(item: Item) {
    if (item.link) {
      if (item.uploadId) api.discardUpload(item.uploadId).catch(() => undefined);
      update(item.key, { state: "waiting", sent: 0, error: undefined, uploadId: undefined });
      api.addLink(item.link)
        .then((added) => update(item.key, { state: "uploading", uploadId: added.upload_id, size: added.size }))
        .catch((e) => update(item.key, { state: "failed", error: e instanceof Error ? e.message : "This link could not be added." }));
      return;
    }
    started.current.delete(item.key);
    update(item.key, { state: "waiting", sent: 0, error: undefined, controller: new AbortController() });
  }

  function toggleOutput(kind: OutputKind) {
    setOptions((o) => ({
      ...o,
      outputs: o.outputs.includes(kind) ? o.outputs.filter((k) => k !== kind) : [...o.outputs, kind],
    }));
  }

  async function start() {
    setStarting(true);
    setError("");
    try {
      await api.createJobs(ready.map((i) => ({ upload_id: i.uploadId!, context: i.context.trim() })), options);
      setItems((list) => list.filter((i) => i.state !== "ready"));
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start these lectures.");
    } finally {
      setStarting(false);
    }
  }

  return (
    <section className="card upload-card">
      <div className="card-head">
        <h2>Add lectures</h2>
        <p className="muted">Drop videos from this computer or paste video links. Uploads and downloads continue in the background and resume if the connection drops.</p>
      </div>

      <div
        className={`dropzone ${dragging ? "dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          addFiles(e.dataTransfer.files);
        }}
        onClick={() => input.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
      >
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
          <path d="M12 16V4m0 0-4 4m4-4 4 4M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <strong>Drop lecture videos here</strong>
        <span className="muted">or click to choose files · {videoTypes.join(", ").replaceAll(".", "").toUpperCase()}</span>
        <input ref={input} type="file" accept={videoTypes.join(",")} multiple hidden onChange={(e) => e.target.files && addFiles(e.target.files)} />
      </div>
      <form className="link-row" onSubmit={(e) => { e.preventDefault(); addLinks(); }}>
        <input
          value={link}
          onChange={(e) => setLink(e.target.value)}
          placeholder="Or paste a video link, e.g. https://….cloudfront.net/…/lecture_720p.mp4"
          aria-label="Video link"
          spellCheck={false}
          autoComplete="off"
        />
        <button className="ghost" type="submit" disabled={!link.trim() || checking}>{checking ? "Checking…" : "Add link"}</button>
      </form>
      {linkError && <p className="error-text small">{linkError}</p>}
      {error && <p className="error-text">{error}</p>}

      {items.length > 0 && (
        <>
          <div className="apply-all">
            <label>
              Subject or chapter for all
              <input
                value={allContext}
                placeholder="e.g. Class 12 Physics · Electrostatics"
                onChange={(e) => {
                  setAllContext(e.target.value);
                  setItems((list) => list.map((i) => ({ ...i, context: e.target.value })));
                }}
              />
            </label>
          </div>
          <ul className="upload-list">
            {items.map((item) => (
              <li key={item.key} className={`upload-item ${item.state}`}>
                <div className="upload-main">
                  <div className="upload-name" title={item.name}>
                    {item.link && <span className="link-tag">link</span>}{item.name}
                  </div>
                  <div className="muted small">
                    {item.size ? bytes(item.size) : ""}
                    {item.duration ? ` · ${clock(item.duration)}` : ""}
                    {item.state === "uploading" && item.size > 0 &&
                      ` · ${Math.floor((item.sent / item.size) * 100)}% ${item.link ? "downloaded" : "uploaded"}`}
                    {item.state === "waiting" && (item.link ? " · checking link" : " · waiting to upload")}
                    {item.state === "ready" && " · ready"}
                  </div>
                  {(item.state === "uploading" || item.state === "waiting") && (
                    <div className="bar"><span style={{ width: `${item.size ? (item.sent / item.size) * 100 : 0}%` }} /></div>
                  )}
                  {item.error && <div className="error-text small">{item.error}</div>}
                </div>
                <input
                  className="upload-context"
                  value={item.context}
                  placeholder="Subject (optional)"
                  onChange={(e) => update(item.key, { context: e.target.value })}
                  aria-label={`Subject for ${item.name}`}
                />
                <div className="upload-actions">
                  {item.state === "failed" && <button className="ghost" onClick={() => retryItem(item)}>Retry</button>}
                  <button className="ghost icon" onClick={() => removeItem(item)} aria-label={`Remove ${item.name}`}>✕</button>
                </div>
              </li>
            ))}
          </ul>

          <div className="options">
            <fieldset>
              <legend>Create these videos</legend>
              <div className="chips">
                {(Object.keys(OUTPUT_TITLES) as OutputKind[]).map((kind) => (
                  <label key={kind} className={`chip ${options.outputs.includes(kind) ? "on" : ""}`}>
                    <input type="checkbox" checked={options.outputs.includes(kind)} onChange={() => toggleOutput(kind)} />
                    {OUTPUT_TITLES[kind]}
                  </label>
                ))}
              </div>
            </fieldset>
            <fieldset>
              <legend>Editing style</legend>
              <div className="segmented">
                <button className={options.mode === "balanced" ? "on" : ""} onClick={() => setOptions({ ...options, mode: "balanced" })}>
                  Complete lessons
                </button>
                <button className={options.mode === "aggressive" ? "on" : ""} onClick={() => setOptions({ ...options, mode: "aggressive" })}>
                  Study essentials
                </button>
              </div>
            </fieldset>
            <details className="advanced">
              <summary>Advanced options</summary>
              <div className="advanced-grid">
                <label>
                  Spoken language
                  <input value={options.language} placeholder="Auto-detect · or Hindi, English" onChange={(e) => setOptions({ ...options, language: e.target.value })} />
                </label>
                <label>
                  Cut quality
                  <select value={options.cut_style} onChange={(e) => setOptions({ ...options, cut_style: e.target.value as JobOptions["cut_style"] })}>
                    <option value="smooth">Precise cuts</option>
                    <option value="fast">Fast cuts (keyframes)</option>
                  </select>
                </label>
                <label className="check"><input type="checkbox" checked={options.visual_review} onChange={(e) => setOptions({ ...options, visual_review: e.target.checked })} />Review uncertain sections using video frames</label>
                <label className="check"><input type="checkbox" checked={options.dry_run} onChange={(e) => setOptions({ ...options, dry_run: e.target.checked })} />Analyse only — skip video export</label>
                <label className="check"><input type="checkbox" checked={options.fresh} onChange={(e) => setOptions({ ...options, fresh: e.target.checked })} />Start fresh — repeat speech recognition and analysis</label>
              </div>
            </details>
          </div>

          <div className="start-row">
            <div className="muted">
              {ready.length} of {items.length} ready
              {estimate && ready.length > 0 && <> · estimated AI cost <strong>₹{estimate.inr}</strong> <span className="small">(based on {estimate.basis})</span></>}
            </div>
            <button className="primary" disabled={!ready.length || !options.outputs.length || starting} onClick={start}>
              {starting ? "Starting…" : `Start ${ready.length || ""} lecture${ready.length === 1 ? "" : "s"}`}
            </button>
          </div>
        </>
      )}
    </section>
  );
}
