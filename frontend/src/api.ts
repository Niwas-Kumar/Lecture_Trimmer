// Typed client for the Lecture Trimmer API (lecture_trimmer/server/api.py).

export type OutputKind = "questions" | "theory" | "combined";
export type JobStatus = "queued" | "waiting" | "running" | "complete" | "partial" | "failed" | "cancelled";
export type Stage = "" | "prepare" | "transcribe" | "analyse" | "render";

export interface Job {
  id: string;
  filename: string;
  options: JobOptions & { context?: string };
  status: JobStatus;
  stage: Stage;
  fraction: number;
  message: string;
  created: number;
  started: number | null;
  finished: number | null;
  outputs: Partial<Record<OutputKind, { status: string; duration?: number }>>;
  cost_inr: string | null;
  duration: number | null;
  error: string | null;
  error_reference: string | null;
  batch: string | null;
}

export interface JobOptions {
  outputs: OutputKind[];
  mode: "balanced" | "aggressive";
  cut_style: "smooth" | "fast";
  language: string;
  visual_review: boolean;
  fresh: boolean;
  dry_run: boolean;
}

export interface Me {
  signed_in: boolean;
  local: boolean;
  mode: string;
  app_version: string;
  analysis_version: string;
  speech: string;
  limits: Record<string, number>;
  video_types: string[];
}

export interface Cut { start: number; end: number; category: string; reason: string }
export interface Section {
  id: number;
  start: number;
  end: number;
  kind: "questions" | "theory" | "nonacademic" | "uncertain";
  role: string | null;
  title: string;
  summary: string;
  evidence: string;
  videos: { kind: OutputKind; share: number; note: string }[];
  checks: string[];
  notes: string[];
  cuts: Cut[];
}

export interface Review {
  duration: number;
  analysis_version: string;
  mode: string;
  subject: string | null;
  sections: Section[];
  totals: Record<string, number>;
  warnings: string[];
  removed: Record<string, number>;
  dead_air: Record<string, number>;
  timings: Record<string, number>;
  ai_usage: { reported_cost_inr?: string; requests?: number; models?: string[] } | null;
  files: string[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { ...(init.body && typeof init.body === "string" ? { "Content-Type": "application/json" } : {}), ...init.headers },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  me: () => request<Me>("/api/me"),
  jobs: () => request<{ jobs: Job[] }>("/api/jobs").then((r) => r.jobs),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  review: (id: string) => request<Review>(`/api/jobs/${id}/review`),
  estimate: (seconds: number) =>
    request<{ inr: number; inr_per_hour: number; basis: string }>(`/api/estimate?seconds=${Math.round(seconds)}`),
  createJobs: (lectures: { upload_id: string; context: string }[], options: JobOptions) =>
    request<{ batch: string; jobs: Job[] }>("/api/jobs", { method: "POST", body: JSON.stringify({ lectures, options }) }),
  cancel: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: "POST" }),
  retry: (id: string) => request<Job>(`/api/jobs/${id}/retry`, { method: "POST" }),
  remove: (id: string) => request<{ ok: boolean }>(`/api/jobs/${id}`, { method: "DELETE" }),
  discardUpload: (id: string) => request<{ ok: boolean }>(`/api/uploads/${id}`, { method: "DELETE" }),
  addLink: (url: string) =>
    request<{ upload_id: string; filename: string; size: number }>("/api/links", { method: "POST", body: JSON.stringify({ url }) }),
  uploadStatus: (id: string) => request<UploadStatus>(`/api/uploads/${id}`),
  updateStatus: () => request<UpdateStatus>("/api/update"),
  checkForUpdate: (link: string) =>
    request<{ message: string }>("/api/update/check", { method: "POST", body: JSON.stringify({ link }) }),
  restartForUpdate: () => request<{ message: string }>("/api/update/restart", { method: "POST" }),
  supportReport: async () => {
    const response = await fetch("/api/support", { method: "POST", credentials: "same-origin" });
    if (!response.ok) throw new ApiError(response.status, "The support report could not be saved. Please retry.");
    return response.blob();
  },
};

export interface UpdateStatus {
  installed: boolean;
  version: string;
  pending: string | null;
  link_saved: boolean;
}

export interface UploadStatus {
  upload_id: string;
  received: number;
  size: number;
  complete: boolean;
  filename: string;
  duration: number | null;
  error: string | null;
  downloading: boolean;
}

export const mediaUrl = (id: string, kind: OutputKind | "source", download = false) =>
  `/api/jobs/${id}/media/${kind}${download ? "?download=1" : ""}`;
export const fileUrl = (id: string, name: string) => `/api/jobs/${id}/files/${name}`;

/** Live job list; the browser reconnects automatically if the stream drops. */
export function subscribeJobs(onJobs: (jobs: Job[]) => void, onState: (connected: boolean) => void) {
  const source = new EventSource("/api/events");
  source.addEventListener("jobs", (event) => {
    onState(true);
    onJobs(JSON.parse((event as MessageEvent).data).jobs);
  });
  source.onopen = () => onState(true);
  source.onerror = () => onState(false);
  return () => source.close();
}

export interface UploadProgress { sent: number; total: number }

/**
 * Upload one file in chunks. After a network error it asks the server how much
 * arrived and continues from there, so a large lecture never restarts from zero.
 */
export async function uploadFile(
  file: File,
  onProgress: (p: UploadProgress) => void,
  signal: AbortSignal,
): Promise<{ upload_id: string; duration: number }> {
  const start = await request<{ upload_id: string; received: number; chunk_bytes: number }>("/api/uploads", {
    method: "POST",
    body: JSON.stringify({ filename: file.name, size: file.size }),
    signal,
  });
  const id = start.upload_id;
  const chunk = start.chunk_bytes;
  let offset = 0;
  let failures = 0;
  while (offset < file.size) {
    if (signal.aborted) throw new DOMException("Upload cancelled", "AbortError");
    const end = Math.min(file.size, offset + chunk);
    try {
      const response = await fetch(`/api/uploads/${id}?offset=${offset}`, {
        method: "PUT",
        body: file.slice(offset, end),
        credentials: "same-origin",
        signal,
      });
      if (response.status === 409) {
        offset = (await response.json()).received;
      } else if (!response.ok) {
        throw new ApiError(response.status, (await response.json().catch(() => ({}))).detail || "Upload failed");
      } else {
        offset = (await response.json()).received;
        failures = 0;
      }
      onProgress({ sent: offset, total: file.size });
    } catch (error) {
      if (signal.aborted || (error instanceof ApiError && error.status < 500 && error.status !== 409)) throw error;
      if (++failures > 8) throw error;
      await new Promise((resolve) => setTimeout(resolve, Math.min(15000, 1000 * 2 ** failures)));
      const status = await request<{ received: number }>(`/api/uploads/${id}`, { signal }).catch(() => null);
      if (status) offset = status.received;
    }
  }
  const done = await request<{ upload_id: string; duration: number }>(`/api/uploads/${id}/complete`, {
    method: "POST",
    signal,
  });
  return done;
}
