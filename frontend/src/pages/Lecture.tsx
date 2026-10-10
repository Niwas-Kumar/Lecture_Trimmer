import { useEffect, useMemo, useRef, useState } from "react";
import { api, fileUrl, Job, mediaUrl, OutputKind, Review, Section } from "../api";
import { KIND_LABEL, Timeline } from "../components/Timeline";
import { clock, duration, elapsed, isActive, OUTPUT_TITLES, rupees, STATUS_TEXT } from "../format";

const ROLE_NAMES: Record<string, string> = {
  concept: "Concept explanation",
  illustration: "Example explaining a concept",
  task: "Posed question / exercise",
  task_doubt: "Doubt about a question",
  concept_doubt: "Concept doubt",
  recap: "Recap",
  off_topic: "Off-topic",
  logistics: "Logistics",
  technical: "Technical issue",
  waiting: "Waiting",
  unclear: "Unclear",
};
const FILE_NAMES: Record<string, string> = {
  "transcript.txt": "Transcript (TXT)",
  "transcript.srt": "Subtitles (SRT)",
  "transcript.json": "Transcript (JSON)",
  "analysis.json": "Analysis report",
  "review.html": "Review page (offline)",
};
type Filter = "all" | "flagged" | Section["kind"];

export function LecturePage({ job, now, onBack }: { job: Job | undefined; now: number; onBack: () => void }) {
  const [review, setReview] = useState<Review | null>(null);
  const [reviewError, setReviewError] = useState("");
  const [tab, setTab] = useState<OutputKind | null>(null);
  const [current, setCurrent] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const player = useRef<HTMLVideoElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Transcript files exist as soon as speech recognition finishes; the AI decisions follow.
  const transcribed = !!job && (["analyse", "render"].includes(job.stage) || ["complete", "partial", "failed"].includes(job.status));
  const active = !!job && isActive(job);
  useEffect(() => {
    if (!job || !transcribed) return;
    api.review(job.id).then((r) => {
      setReview(r);
      setReviewError("");
    }).catch((e) => setReviewError(e.message));
  }, [job?.id, transcribed, job?.stage, job?.status]);

  const ready = useMemo(
    () => (job ? (Object.keys(OUTPUT_TITLES) as OutputKind[]).filter((k) => job.outputs[k]?.status === "ready") : []),
    [job],
  );
  useEffect(() => {
    if (!tab || !ready.includes(tab)) setTab(ready[0] ?? null);
  }, [ready, tab]);

  const sections = review?.sections ?? [];
  const playing = sections.find((s) => current >= s.start && current < s.end)?.id ?? null;
  const shown = sections.filter((s) =>
    filter === "all" ? true : filter === "flagged" ? s.checks.length > 0 : s.kind === filter,
  );
  const flagged = sections.filter((s) => s.checks.length).length;

  useEffect(() => {
    // Scroll only the list: scrollIntoView would also move the page and pull the player out of view.
    const list = listRef.current;
    const row = list?.querySelector<HTMLElement>(`[data-id="${selected ?? playing}"]`);
    if (!list || !row) return;
    const box = list.getBoundingClientRect(), r = row.getBoundingClientRect();
    if (r.top < box.top || r.bottom > box.bottom) {
      list.scrollTo({ top: Math.max(0, list.scrollTop + r.top - box.top - 8), behavior: "smooth" });
    }
  }, [selected, playing]);

  function seek(seconds: number, sectionId?: number) {
    const video = player.current;
    if (!video) return;
    video.currentTime = seconds;
    video.play().catch(() => undefined);
    setSelected(sectionId ?? sections.find((s) => seconds >= s.start && seconds < s.end)?.id ?? null);
    video.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  if (!job) {
    return (
      <div className="page">
        <button className="ghost" onClick={onBack}>← All lectures</button>
        <p className="muted">This lecture is not in your list.</p>
      </div>
    );
  }

  return (
    <div className="page lecture-page">
      <div className="lecture-head">
        <button className="ghost" onClick={onBack}>← All lectures</button>
        <div className="lecture-title">
          <h1 title={job.filename}>{job.filename}</h1>
          <div className="muted small job-meta">
            <span className={`pill ${job.status}`}>{STATUS_TEXT[job.status]}</span>
            {job.duration ? <span>{clock(job.duration)} lecture</span> : null}
            {job.options.context ? <span>{job.options.context}</span> : null}
            {job.started ? <span>Processing {elapsed(job, now)}</span> : null}
            {job.cost_inr && Number(job.cost_inr) > 0 ? <span>AI cost {rupees(job.cost_inr)}</span> : null}
          </div>
        </div>
      </div>

      {isActive(job) && (
        <section className="card">
          <div className="bar large"><span style={{ width: `${Math.round(job.fraction * 100)}%` }} /></div>
          <p className="small">{job.message}</p>
          <p className="muted small">You can close this page; processing continues and results appear here when ready.</p>
        </section>
      )}
      {job.status === "failed" && (
        <section className="card error-card">
          <strong>This lecture could not be finished.</strong>
          <p className="small">{job.error}{job.error_reference && <> · Reference {job.error_reference}</>}</p>
        </section>
      )}

      {ready.length > 0 && tab && (
        <section className="card">
          <div className="tabs" role="tablist">
            {ready.map((kind) => (
              <button key={kind} role="tab" aria-selected={tab === kind} className={tab === kind ? "on" : ""} onClick={() => setTab(kind)}>
                {OUTPUT_TITLES[kind]} <span className="muted small">{duration(job.outputs[kind]?.duration)}</span>
              </button>
            ))}
          </div>
          <video key={tab} className="result-video" controls preload="metadata" src={mediaUrl(job.id, tab)} />
          <div className="row-between">
            <span className="muted small">Saved on this computer. Download keeps a copy wherever you like.</span>
            <a className="button primary" href={mediaUrl(job.id, tab, true)} download>
              Download {OUTPUT_TITLES[tab].toLowerCase()}
            </a>
          </div>
        </section>
      )}

      <section className="card review-card">
        <div className="card-head">
          <h2>AI decisions</h2>
          <p className="muted">Each block is a part of the original lecture, coloured by the video it went into. Click any block or row to watch it.</p>
        </div>
        {(active ? sections.length === 0 : !review && !reviewError) && (
          <p className="muted">{active ? "Available once the AI analysis finishes." : "Loading…"}</p>
        )}
        {reviewError && !active && <p className="muted">{reviewError}</p>}
        {review && sections.length === 0 && !active && (
          <p className="muted">Section decisions are available for lectures analysed with the current version. Retry this lecture to see them.</p>
        )}
        {review && sections.length > 0 && (
          <>
            <video
              ref={player}
              className="source-video"
              controls
              preload="metadata"
              src={mediaUrl(job.id, "source")}
              onTimeUpdate={(e) => setCurrent(e.currentTarget.currentTime)}
            />
            <Timeline sections={sections} duration={review.duration} current={current} selected={selected ?? playing} onSeek={seek} />
            <div className="legend small">
              {(["questions", "theory", "nonacademic", "uncertain"] as const).filter((kind) => kind !== "uncertain" || review.totals[kind] > 0).map((kind) => (
                <span key={kind}><i className={`swatch ${kind}`} />{KIND_LABEL[kind]} {duration(review.totals[kind] || 0)}</span>
              ))}
              <span><i className="swatch cut" />Cut inside a section</span>
            </div>
            <div className="filters" role="tablist">
              {(["all", "flagged", "questions", "theory", "nonacademic"] as Filter[]).map((f) => (
                <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
                  {f === "all" ? `All ${sections.length}` : f === "flagged" ? `⚠ Check ${flagged}` : KIND_LABEL[f as Section["kind"]]}
                </button>
              ))}
            </div>
            <div className="section-list" ref={listRef}>
              {shown.map((s) => (
                <button
                  key={s.id}
                  data-id={s.id}
                  className={`section-row ${s.kind} ${(selected ?? playing) === s.id ? "active" : ""}`}
                  onClick={() => seek(s.start, s.id)}
                >
                  <span className="section-time">{clock(s.start)}<br /><span className="muted">{duration(s.end - s.start)}</span></span>
                  <span className="section-body">
                    <span className="section-top">
                      <span className={`tag ${s.kind}`}>{KIND_LABEL[s.kind]}</span>
                      <span className="muted small">{ROLE_NAMES[s.role || ""] || s.role || ""}</span>
                    </span>
                    <strong>{s.title}</strong>
                    {s.summary && <span className="small">{s.summary}</span>}
                    {s.evidence && <span className="quote small">“{s.evidence}”</span>}
                    {s.checks.map((c, i) => <span key={i} className="check small">⚠ {c}</span>)}
                    {s.notes.map((n, i) => <span key={i} className="note small">ℹ {n}</span>)}
                    {s.cuts.map((c, i) => (
                      <span key={`c${i}`} className="cut-note small">
                        ✂ {clock(c.start)}–{clock(c.end)} removed as {c.category}{c.reason ? `: ${c.reason}` : ""}
                      </span>
                    ))}
                    <span className="muted small">
                      In: {s.videos.length ? s.videos.map((v) => OUTPUT_TITLES[v.kind] + (v.note ? " (context)" : "")).join(", ") : "no video"}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          </>
        )}
      </section>

      {review && (
        <section className="card">
          <div className="card-head"><h2>{active ? "Files so far" : "Details"}</h2></div>
          {!active && <div className="stats">
            <div><span className="muted small">Removed as unrelated</span><strong>{duration(Object.values(review.removed).reduce((a, b) => a + b, 0))}</strong></div>
            <div><span className="muted small">Dead air shortened</span><strong>{duration(review.dead_air.combined ?? Math.max(0, ...Object.values(review.dead_air)))}</strong></div>
            <div><span className="muted small">AI analysis</span><strong>{duration(review.timings.analysis_seconds)}</strong></div>
            <div><span className="muted small">Rendering</span><strong>{duration(review.timings.render_seconds)}</strong></div>
            <div><span className="muted small">Subject notes</span><strong>{review.subject ? review.subject.replace("_", " ") : "general"}</strong></div>
            <div><span className="muted small">Analysis version</span><strong className="small">{review.analysis_version}</strong></div>
          </div>}
          {review.files.length > 0 && (
            <div className="files">
              {review.files.map((name) => (
                <a key={name} className="button ghost small" href={fileUrl(job.id, name)} download>{FILE_NAMES[name] || name}</a>
              ))}
            </div>
          )}
          {review.warnings.length > 0 && (
            <details className="notes">
              <summary>Analysis notes ({review.warnings.length})</summary>
              <ul>{review.warnings.map((w, i) => <li key={i} className="small">{w}</li>)}</ul>
            </details>
          )}
        </section>
      )}
    </div>
  );
}
