import { useState } from "react";
import type { Job, Me } from "../api";
import { JobCard } from "../components/JobCard";
import { UploadPanel } from "../components/UploadPanel";
import { isActive } from "../format";

type View = "active" | "finished" | "all";

export function HomePage({ me, jobs, now, connected, open }: {
  me: Me;
  jobs: Job[];
  now: number;
  connected: boolean;
  open: (id: string) => void;
}) {
  const active = jobs.filter(isActive);
  const [view, setView] = useState<View>(active.length ? "active" : "all");
  const finished = jobs.filter((j) => !isActive(j));
  const shown = view === "active" ? active : view === "finished" ? finished : jobs;

  return (
    <div className="page home">
      <UploadPanel videoTypes={me.video_types} onStarted={() => setView("active")} />

      <section className="card">
        <div className="card-head row-between">
          <div>
            <h2>Your lectures</h2>
            <p className="muted small">
              Up to {me.limits.analyse} lectures are analysed at once and one renders at a time, so a batch finishes as fast as your computer allows.
              {!connected && " · Reconnecting to live updates…"}
            </p>
          </div>
          <div className="segmented small">
            <button className={view === "active" ? "on" : ""} onClick={() => setView("active")}>In progress {active.length}</button>
            <button className={view === "finished" ? "on" : ""} onClick={() => setView("finished")}>Finished {finished.length}</button>
            <button className={view === "all" ? "on" : ""} onClick={() => setView("all")}>All {jobs.length}</button>
          </div>
        </div>
        {shown.length === 0 ? (
          <div className="empty muted">
            {jobs.length === 0 ? "No lectures yet. Add videos above to begin." : "Nothing here right now."}
          </div>
        ) : (
          <div className="job-grid">
            {shown.map((job) => <JobCard key={job.id} job={job} now={now} onOpen={() => open(job.id)} />)}
          </div>
        )}
      </section>
    </div>
  );
}
