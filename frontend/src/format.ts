import type { Job, OutputKind } from "./api";

export function clock(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return "–";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return "–";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m} min`;
  return `${s}s`;
}

export function bytes(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  return `${Math.max(1, Math.round(n / 1024 ** 2))} MB`;
}

export const rupees = (value: string | number | null | undefined) =>
  value == null || value === "" ? "–" : `₹${Number(value).toFixed(2)}`;

export const OUTPUT_TITLES: Record<OutputKind, string> = {
  questions: "Question solving",
  theory: "Theory",
  combined: "Theory + questions",
};

export const STATUS_TEXT: Record<Job["status"], string> = {
  queued: "Queued",
  waiting: "Waiting",
  running: "Processing",
  complete: "Ready",
  partial: "Partly ready",
  failed: "Failed",
  cancelled: "Stopped",
};

export const isActive = (job: Job) => ["queued", "waiting", "running"].includes(job.status);

export function elapsed(job: Job, now: number): string {
  if (!job.started) return "";
  const end = job.finished ?? now / 1000;
  return clock(end - job.started);
}
