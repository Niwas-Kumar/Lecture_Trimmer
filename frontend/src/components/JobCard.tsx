import { useState } from "react";
import { api, Job } from "../api";
import { clock, duration, elapsed, isActive, OUTPUT_TITLES, rupees, STATUS_TEXT } from "../format";

const STEPS = [
  { key: "prepare", label: "Prepare" },
  { key: "transcribe", label: "Transcribe" },
  { key: "analyse", label: "AI analysis" },
  { key: "render", label: "Render" },
] as const;

function stepState(job: Job, index: number): "done" | "now" | "todo" {
  if (job.status === "complete" || job.status === "partial") return "done";
  const current = STEPS.findIndex((s) => s.key === job.stage);
  if (current < 0) return job.started && index === 0 && isActive(job) ? "now" : "todo";
  if (index < current) return "done";
  return index === current ? "now" : "todo";
}

export function JobCard({ job, now, onOpen }: { job: Job; now: number; onOpen: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = isActive(job);
  const finished = job.status === "complete" || job.status === "partial";

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={`job-card status-${job.status}`}>
      <div className="job-top">
        <button className="job-title linklike" onClick={onOpen} title={job.filename}>{job.filename}</button>
        <span className={`pill ${job.status}`}>{STATUS_TEXT[job.status]}</span>
      </div>
      <div className="muted small job-meta">
        {job.options.context ? <span>{job.options.context}</span> : null}
        {job.duration ? <span>{clock(job.duration)} lecture</span> : null}
        {job.started ? <span>{elapsed(job, now)} elapsed</span> : null}
        {job.cost_inr && Number(job.cost_inr) > 0 ? <span>AI {rupees(job.cost_inr)}</span> : null}
      </div>

      {(active || finished) && (
        <ol className="steps" aria-label="Progress">
          {STEPS.map((step, i) => (
            <li key={step.key} className={stepState(job, i)}>
              <span className="dot" />
              {step.label}
            </li>
          ))}
        </ol>
      )}

      {active && (
        <>
          <div className="bar"><span style={{ width: `${Math.round(job.fraction * 100)}%` }} /></div>
          <div className="small job-message">{job.message}</div>
        </>
      )}

      {finished && (
        <div className="job-outputs small">
          {Object.entries(job.outputs).map(([kind, item]) => (
            <span key={kind} className={`out ${item?.status}`}>
              {OUTPUT_TITLES[kind as keyof typeof OUTPUT_TITLES]}:{" "}
              {item?.status === "ready" ? duration(item.duration) : item?.status === "no_matching_content" ? "no matching content" : item?.status}
            </span>
          ))}
        </div>
      )}

      {job.status === "failed" && (
        <div className="error-text small">
          {job.error || "This lecture could not be finished."}
          {job.error_reference && <> · Reference {job.error_reference}</>}
        </div>
      )}
      {job.status === "cancelled" && <div className="muted small">{job.message}</div>}
      {error && <div className="error-text small">{error}</div>}

      <div className="job-actions">
        {finished && <button className="primary small" onClick={onOpen}>Open results</button>}
        {active && <button className="ghost small" disabled={busy} onClick={() => run(() => api.cancel(job.id))}>Stop</button>}
        {(job.status === "failed" || job.status === "cancelled" || job.status === "partial") && (
          <button className="ghost small" disabled={busy} onClick={() => run(() => api.retry(job.id))}>Retry</button>
        )}
        {!active && (
          <button className="ghost small danger" disabled={busy} onClick={() => confirm(`Remove ${job.filename} from the list? Saved videos stay on disk.`) && run(() => api.remove(job.id))}>
            Remove
          </button>
        )}
      </div>
    </article>
  );
}
