import type { Section } from "../api";
import { clock } from "../format";

export const KIND_LABEL: Record<Section["kind"], string> = {
  questions: "Questions",
  theory: "Theory",
  nonacademic: "Removed",
  uncertain: "Needs review",
};

interface Props {
  sections: Section[];
  duration: number;
  current: number;
  selected: number | null;
  onSeek: (seconds: number, sectionId?: number) => void;
}

/** The whole original lecture, coloured by where each part went. Click to watch. */
export function Timeline({ sections, duration, current, selected, onSeek }: Props) {
  const total = Math.max(duration, 1);
  const pct = (t: number) => `${(100 * t) / total}%`;

  return (
    <div className="timeline-wrap">
      <div
        className="timeline"
        onClick={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          onSeek(((e.clientX - box.left) / box.width) * total);
        }}
        role="slider"
        aria-label="Lecture timeline"
        aria-valuemin={0}
        aria-valuemax={Math.round(total)}
        aria-valuenow={Math.round(current)}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") onSeek(Math.min(total, current + 10));
          if (e.key === "ArrowLeft") onSeek(Math.max(0, current - 10));
        }}
      >
        {sections.map((s) => (
          <button
            key={s.id}
            className={`seg ${s.kind} ${selected === s.id ? "selected" : ""} ${s.checks.length ? "flagged" : ""}`}
            style={{ left: pct(s.start), width: `max(2px, ${pct(s.end - s.start)})` }}
            title={`${clock(s.start)}–${clock(s.end)} · ${KIND_LABEL[s.kind]} · ${s.title}${s.checks.length ? " · ⚠ check" : ""}`}
            onClick={(e) => {
              e.stopPropagation();
              onSeek(s.start, s.id);
            }}
          />
        ))}
        {sections.flatMap((s) =>
          s.cuts.map((c, i) => (
            <span key={`${s.id}-${i}`} className="cut" style={{ left: pct(c.start), width: `max(1px, ${pct(c.end - c.start)})` }} />
          )),
        )}
        <span className="playhead" style={{ left: pct(current) }} />
      </div>
      <div className="timeline-scale small muted">
        <span>0:00</span>
        <span>{clock(total / 2)}</span>
        <span>{clock(total)}</span>
      </div>
    </div>
  );
}
