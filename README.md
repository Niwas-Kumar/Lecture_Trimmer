# Lecture Trimmer

Create three study videos from one lecture analysis:

- **Question solving:** complete exercises, their statements, subparts, solution steps, final answers, doubts and short theory explanations needed by those exercises. These explanations stay in place.
- **Theory:** concepts, definitions, formulas, general derivations/proofs, illustrative examples that explain a topic, and standalone conceptual doubts. Numbers alone do not turn a concept example into an exercise.
- **Theory + questions:** both kinds in the original lecture order, with chatter, announcements and technical interruptions removed.

All three are selected by default. Choose just one or two if that is all you need. Transcription and classification happen once per run, regardless of the number of outputs. A category with no matching content shows an explicit message and produces no empty video.

Exports contain study only. Homework-submission polls, motivation, leaderboards, platform logistics and closing chat are excluded; actual homework problems and closing academic doubts are preserved. A nonacademic map must be confirmed by the editor with cuts, or explicitly corrected into complete academic/nonacademic subactivities. Conflicts trigger a bounded correction request. Unresolved categories stop publication with an actionable review message instead of being inserted into both outputs. Silent board work inside teaching stays protected; that protection does not automatically extend to a whole poll or closing announcement. Existing transcripts can be reused, but analysis from before `lessons-v5.2-study-only` is not reused.

## Run locally

For Google desktop login using PW's private `Lecture_Trimmer` email column,
follow [PW_DESKTOP_SETUP.md](docs/PW_DESKTOP_SETUP.md). This mode uses Gemini through
the PW proxy for classification (Gemini 3.5 Flash).

Speech recognition always uses Groq `whisper-large-v3-turbo`. Source runs read `GROQ_API_KEY`
from `.env`; the desktop installer packages the owner's key (see the desktop release guide).
Without a key, lectures cannot be transcribed and the app says so before uploading anything.

Requires Python 3.11+ and both `ffmpeg` and `ffprobe` on PATH.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `GROQ_API_KEY` for speech recognition and one classification key (`GOOGLE_API_KEY` or `ANTHROPIC_API_KEY`). Groq's free tier allows about 8 hours of audio per day and 2 per hour. Do not leave placeholder keys filled in for providers you are not using.

```powershell
python app.py
```

This opens the studio in your browser. Add lectures (files or video links), choose the videos and editing style, and start. Each lecture's page has its videos, downloads, the AI-decision timeline and the analysis report (every unit, label, confidence, reason and each output's source-time ranges).

Timestamped speech can be downloaded as TXT, SRT subtitles or JSON as soon as transcription
finishes, even if later analysis fails. Times refer to the original lecture.

In PW desktop mode each lecture card shows the rupee cost returned by PW for its AI analysis; it is saved in `analysis.json`, including on failure. All selected videos share this cost. Retries are counted. Missing billing values are never assumed free. Groq speech billing is separate and not included.

**Help & updates** (header) shows the version, checks for and applies signed desktop updates (installed copies), and saves a support report with your error records only, never videos, transcripts or credentials.

## The studio interface (batch processing)

Signing in opens **Lecture Trimmer Studio** at `/studio/` (local mode: `http://127.0.0.1:7860/`). The old
Gradio page was removed; lectures processed there are listed in the studio automatically.

- **Add many lectures at once.** Drag in several videos; uploads run two at a time in 16 MB chunks and resume
  from where they stopped after a network drop. Set a subject per lecture or for all, choose the videos and
  editing style, and see the estimated AI cost (learned from your previous lectures) before starting.
- **Or paste video links.** A direct video link (for example a CloudFront or S3 `…_720p.mp4` URL, signed
  or not) is downloaded by the app, in 4 parallel pieces when the server supports it, each resumed after a
  dropped connection. Several links can be pasted at once. Only public internet addresses are fetched (never
  this computer or a private network), the server must answer with a video, and a link's signature
  (`?Expires=…&Signature=…`) is never saved or logged. An expired link says so; copy a fresh one.
- **An assembly line, not a waiting line.** Each lecture enters a stage only when that stage has a free slot:
  2 preparing, 2 transcribing, `TRIMMER_PARALLEL_ANALYSES` (default 3) in AI analysis, 1 rendering on the GPU.
  All lectures share `TRIMMER_AI_MAX_CONCURRENT_REQUESTS` (default 6) so PW is never flooded. While one lecture
  renders, the next ones are transcribed and analysed.
- **Nothing is lost.** The queue is saved in `jobs/`; refreshing, closing the browser or restarting the app
  keeps every lecture, and interrupted lectures resume from saved work when you return. One lecture failing
  never stops the others; Retry reuses everything already paid for.
- **Results and review.** Each lecture page has the three videos with downloads, and the AI decisions
  timeline above the original-lecture player: click any section (or anywhere on the bar) to watch it; flagged
  sections can be filtered with **⚠ Check**.

The interface is React + TypeScript in `frontend/` (development only, never shipped). Rebuild after changing it:

```powershell
cd frontend
npm install        # first time only
npm run build      # writes lecture_trimmer/web/dist/, which ships in releases
npm run dev        # optional live-reload UI against a running app on port 7861
```

## Cut quality and classification

**Precise cuts** (`smooth` in the CLI) is the default. It uses speech timestamps, small boundary padding, H.264 encoding, and 40 ms audio fades. The app tests NVIDIA NVENC, then Intel Quick Sync, and falls back to libx264 when hardware is unavailable or fails. Override with `TRIMMER_ENCODER=libx264` if needed. Shared source sections are encoded once and reused by all three videos; the combined output normally only joins those sections and encodes audio. Frame-aligned segments and lossless intermediate audio avoid accumulated timing drift. AAC is encoded once per output. No visual dissolves obscure formulas or board text.

**Fast cuts** stream-copy only when keyframes allow the academic boundaries to be respected. Otherwise the pipeline automatically uses precise cuts and explains why in the report. It never widens an academic selection to restore rejected content just to save encoding time.

**Complete lessons** (`balanced`, the default) preserves connected explanations, pedagogical restatements, translations, derivations and complete solutions; it trims only empty filler, waiting and word-for-word repeats, and shortens dead air over 3 s to 1.2 s. **Study essentials** (`aggressive`) also trims re-explanations once a point is established and shortens pauses over 2 s to 0.8 s. Neither mode forces a target duration or retention percentage.

### How the AI decides (analysis `editor-v6-roles`)

The prompts brief the model like a video editor who knows nothing about teaching: who watches (a student revising for an exam, for whom a lost formula is worse than 20 extra seconds), how a live class runs, and contrast examples from physics, chemistry, maths, biology, history and English. A subject pack and class-type note (revision, doubt class, test discussion) are chosen deterministically from the subject hint or transcript keywords.

The model reports what **observably happened** in each section — `concept`, `illustration`, `task`, `task_doubt`, `concept_doubt`, `recap`, `off_topic`, `logistics`, `technical`, `waiting` or `unclear` — and must quote the task statement for a question. The program, not the model, maps roles to videos: tasks and task doubts go to **Questions**; concepts, illustrations, recaps and concept doubts go to **Theory**. Set `TRIMMER_ILLUSTRATIONS_IN_QUESTIONS=1` to also include teaching illustrations in the questions video.

Editing has two stages. Stage 1 removes unrelated passages (strict validation, as before). Stage 2 tightens kept teaching: whole units of filler, waiting or empty repetition. The program enforces a clean cut — no essential unit, no stranded piece under 5 s, at most 35 % of a section — by dropping a polish cut that breaks a rule, never by failing the lecture.

Measured audio pauses guide the model (`<pause 7s>` markers) and every cut point is moved into the nearest silence so words are not clipped. The silence threshold is relative to each lecture's loudness. `TRIMMER_TIGHTEN_PAUSES=0` keeps pauses exactly as recorded.

### Review the AI's decisions

Open **AI decisions — which parts went into Questions and Theory** below the results. A timeline of the original lecture is coloured by destination (Questions, Theory, Removed, Needs review), with hatched marks for cuts inside a section. The table shows each section's role, the model's reason, the question it quoted and every internal cut. Sections are flagged ⚠ when a question could not be found in the transcript, when a non-question section runs over 12 minutes, or when the model asked for review. Pick a section to watch that part of the original. Each job also writes `review.html` — the same view with a full lecture player where clicking a time jumps to it (works on the computer that processed the lecture).

The current pipeline maps complete learning activities, then reviews whole passages inside those activities. It removes independently identified unrelated talk, logistics and interruptions. Real word timestamps are used when available; incomplete word alignment falls back to original ASR segments rather than inventing cut times. Evidence units are about four seconds, short enough that a filler phrase is its own unit for stage-2 tightening. Hindi, English and Hinglish are supported by the prompts; subject context helps distinguish an academic story or demonstration from unrelated chatter.

Each output includes the prerequisites that its selected activities explicitly need, but background from the other category is copied in only when it is at most 3 minutes (`TRIMMER_MAX_CONTEXT_SECONDS`); longer background stays in its own video and the review panel names it. A formula explanation inside an exercise stays with that exercise. A necessary earlier setup may appear in both videos; the UI and report disclose shared context. The combined video is a chronological union with no repeated source intervals. A short complete answer survives; a proposed cut that would strand an incomplete tiny fragment requires corrected boundaries or explicit complete subactivities.

Silent or untranscribed intervals of at least eight seconds are explicit evidence, so board work is not dropped before analysis. Optional **selected-frame review** sends up to 24 local JPEG samples across up to eight uncertain activities to Gemini in one batch. It never uploads the entire video for this step. Frames may clarify or rescue teaching; a static board cannot prove chatter is academic. Untranscribed board work inside a classified teaching activity is retained and flagged; unresolved activity categories require correction before export. This is bounded visual sampling, not continuous video understanding, and it may miss brief visual events.

The default Gemini classifier is `gemini-3.5-flash`; `GEMINI_MODEL` overrides it. There is no automatic downgrade to a lighter model. Validated maps, editorial batches and frame evidence are cached. Invalid IDs, overlaps, omitted activities or unsafe dependencies trigger a bounded correction request, then fail visibly. Completed work survives retries.

NVIDIA exports use four workers by default (Intel Quick Sync two; `TRIMMER_RENDER_WORKERS=1..4`); software encoding uses one. Joined audio uses the fast AAC coder at 128 kbps, about three times quicker than the default coder and still double the bitrate of typical lecture sources. Unique source sections are encoded once across the three outputs. Exact repeat exports reuse a checksum-verified completed MP4 in a new job directory. Precise exports use PCM intermediates and encode AAC once after joining. Before publication, the app checks every decoded audio frame for coverage, continuity and synchronization; container duration alone is not considered sufficient.

Reports include complete activities, whole-passage cuts, necessary shared context, uncertainty flags, optional visual evidence metadata, stage timings and section lengths. Classification still depends on ASR and the model. This release has regression tests and a real physics-lecture check, not a measured accuracy guarantee across all teachers and subjects. A representative human-reviewed evaluation set is still needed before broad rollout.

## CLI

```powershell
python main.py lecture.mp4
python main.py lecture.mp4 --outputs questions
python main.py lecture.mp4 --outputs theory combined --cut-style fast
python main.py lecture.mp4 --mode aggressive --context "Class 12 Physics, Electrostatics"
python main.py lecture.mp4 --classifier gemini --language Hindi
python main.py lecture.mp4 --visual-review --dry-run
python main.py lecture.mp4 --fresh
python main.py lecture.mp4 --outputs combined --out output/my_lecture.mp4
```

`--keep` optionally chooses academic categories for the combined video; it cannot bypass editorial cuts or include nonacademic labels. `--fresh` / `--retranscribe` bypass transcription and analysis caches. `--dry-run` writes the report without videos. `--out` must be an MP4 path distinct from the input.

## Files and data flow

```text
app.py / main.py                      stable launch commands
lecture_trimmer/
    ui/                              interface, progress, styling
    processing/                      pipeline, cut planning, cancellation
    analysis/                        teaching activities, prompts, visual evidence
    media/                           audio, transcription, video rendering
    auth/                            Google sign-in and private file access
    integrations/                    PW proxy and AI usage accounting
    runtime/                         private diagnostics and support reports
    config.py / models.py / paths.py  shared settings, schemas, locations
tools/                               desktop build and release tooling
desktop_runtime/                     desktop paths, updater, frozen dependencies
vendor/                              externally supplied PW access kit
tests/                               offline regression and FFmpeg tests
docs/                                architecture and setup guides
assets/                              logo and icons
```

Local-only folders (gitignored, never shipped; safe to ignore when reading code):

```text
input/                               your source lecture videos
output/                              finished videos and reports, one folder per job
temp/                                transcripts, analysis caches, reusable work
logs/  support/                      error logs and saved support reports
build/  dist/  releases/             desktop build output (PyInstaller specs go in build/)
.env  client_secret*.json            private keys; must stay at the root
.release-private/                    release-signing key
```

Read [Architecture](docs/ARCHITECTURE.md) for the data flow and stable desktop
boundary, and [Contributing](docs/CONTRIBUTING.md) for development and testing.
Only `app.py` and `main.py` remain as root Python entry points. Older desktop
adapters live in `tools/desktop_compat/` and are mapped to the required locations
by the release builder. Application changes belong in `lecture_trimmer/`. The CLI also runs with
`python -m lecture_trimmer`.

Generated data stays in its existing location:

- `output/<unique-job-id>/<name>_questions.mp4`
- `output/<unique-job-id>/<name>_theory.mp4`
- `output/<unique-job-id>/<name>_combined.mp4`
- `output/<unique-job-id>/analysis.json`
- `temp/sources/<content-hash>.<extension>`: an immutable copy protects active jobs from replaced/expired uploads; identical reuploads share a source/cache identity.
- `temp/transcript_<hash>.json`: keyed by the protected source identity, language, backend, and ASR model settings.
- `temp/lessons_<hash>.json` and `temp/activity_<hash>.json`: complete analysis and independently validated stage caches keyed by evidence, prompts, context, model and visual-review choice.
- `temp/completed_exports/<source-hash>/`: verified completed-video cache; copied into each new job after checksum validation.
- `temp/render_cache/<source-hash>/`: atomically saved sections, keyed by source, exact boundaries, frame rate, encoder and render version. Retrying the same settings reuses completed sections.

Temporary extraction/assembly directories are cleaned after success or failure; protected sources and reusable sections persist. Reports are updated atomically throughout each job, including partial failures. FFmpeg operations with no media progress for three minutes fail visibly. Output jobs have unique directories. The UI queues at most eight waiting jobs and processes one at a time. The CLI exits nonzero on a failed or partial export.

Speech recognition uploads 120-second audio sections to Groq, two at a time, and saves each completed section for retries; rate limits are retried after the wait Groq requests. Transcripts saved by earlier versions are reused. Automatic classification: Claude, then Gemini. The UI and CLI respect `CLASSIFIER_BACKEND`. Speech recognition sends audio sections to Groq; classification sends transcript text and the optional subject hint to its provider. Enabling selected-frame review also sends the selected images to Gemini.

## Share through Google sign-in and a private Sheet

For regular use, host one always-on server behind HTTPS. Users open a browser link, sign in with Google, upload a lecture, and download their own videos. They do not install Python or FFmpeg. Use a private Google Sheet with configurable **email**, **enabled**, and **expires_at** columns to grant access. A server service account reads the Sheet with read-only permissions; it is never published or sent to users.

Google's signed identity token proves who signed in. Typed email addresses are not trusted. Each user's uploads, generated files, reports, and queue sessions have separate authorization, including direct file URLs. Shared mode rejects local paths and disables local saved-job history. Access is rechecked with a bounded 60-second allowlist cache, and a failed refresh denies access instead of keeping stale grants.

See [AUTH_SETUP.md](docs/AUTH_SETUP.md) for the Sheet format, service account, OAuth consent/client setup, HTTPS domain and required environment variables. Then start the server behind your HTTPS reverse proxy:

```powershell
python app.py --auth google-sheet --host 127.0.0.1 --port 7861 --no-browser
```

The normal local command still works without Google authentication. Unauthenticated non-loopback hosting is rejected; Google sign-in needs a stable registered callback URL. An optional local password (`TRIMMER_USER` / `TRIMMER_PASSWORD`) does not enable remote hosting.

## Dedicated-server deployment

Multi-hour uploads and three exports need adequate CPU or supported GPU encoding, disk space and upload bandwidth. Start with a small invited group and one worker, then measure representative lectures. The app queues at most eight waiting jobs. Parallel rendering is bounded by `TRIMMER_RENDER_WORKERS`; it does not create multiple public processing workers.

The Docker image installs Python dependencies and FFmpeg. It defaults to authenticated hosting and exits if its authentication configuration is incomplete. Example for a Linux server with a JSON service-account credential file:

```bash
docker build -t lecture-trimmer .
docker run -d --name lecture-trimmer --restart unless-stopped \
  --env-file .env -p 127.0.0.1:7860:7860 \
  -e TRIMMER_GOOGLE_CREDENTIALS_FILE=/run/secrets/trimmer-google.json \
  -v /secure/server/trimmer-google.json:/run/secrets/trimmer-google.json:ro \
  -v lecture-output:/app/output -v lecture-cache:/app/temp \
  lecture-trimmer
```

Set `TRIMMER_BASE_URL` to the public HTTPS origin and configure its reverse proxy to forward the original Host header. Set `TRIMMER_TRUSTED_PROXY_IPS` to the actual proxy address visible inside the container; the local default is `127.0.0.1`. Configure the proxy's upload limit (up to the app's 5 GB limit), request timeouts, and unbuffered event streaming. Use platform-managed service-account credentials instead of a JSON file where available. Provider and Google secrets stay on the server. `.dockerignore` excludes `.env`, source media, caches and exports from the build context.

**Sources, exports, transcripts and rendering caches persist on server storage.** The operator must manage disk capacity and retention without deleting files used by active jobs. Sessions and exact file grants are in memory: restart signs users out and they must upload again. Existing files remain available to the administrator but are not browsable by other users. There is no automatic crash resumption, per-user billing/quota system, or multi-server coordination yet. Scaling to multiple workers/replicas requires shared session/ownership storage, shared queuing and operational monitoring.

No server has been deployed by this change. Live Google OAuth and private Sheet access need the owner's domain and credentials and must be tested before inviting users. Local simulated-identity tests verify the server authorization paths but are not a substitute for that live setup.

Check provider billing, daily quota, rate limits and spend controls before inviting users. A long lecture may need more requests than a free-tier daily allowance, especially with optional visual checks or retries. Run a representative lecture to measure capacity and cost. Exhausted provider quota prevents new classification until capacity is available; it is not solved by faster video encoding. Retry with the same inputs to reuse saved work rather than selecting **Start fresh**. Saved results show their analysis version and explicitly identify older outputs.

## An EXE for distribution

The Windows desktop host packages Python and FFmpeg and opens the application in
the user's browser. Google desktop login and the PW allowlist control access.
Signed application updates replace code without reinstalling the media runtime.
See the [desktop release guide](docs/DESKTOP_RELEASE_GUIDE.txt). Shared copies
contain the owner's Groq key by the owner's decision, but never other provider keys or
service-account credentials.
Source refactors do not automatically replace an installed version or publish
an update to Google Drive.

## Verification

```powershell
python -m unittest discover -s tests -v
```

Tests cover classification coverage, real speech boundaries, category leakage, short answers, immutable uploads, shared rendering, partial failures, immediate result delivery, empty categories, dry runs, and real FFmpeg exports with many joins and audio/video timing checks. They make no provider API calls. `tests/classification_examples.json` contains assistant-authored examples for optional provider evaluation; such a small example set is not a real-lecture accuracy benchmark.

### Measure quality, not impressions

`tools/evaluate_runs.py` turns prompt changes into numbers:

```powershell
# Stability: do two analyses of the same lecture agree? (v5.1 vs v5.2 agreed on only 36% of seconds)
python tools/evaluate_runs.py compare output/<jobA>/analysis.json output/<jobB>/analysis.json
# Build a hand-checked reference from a run, correct its labels and mark must-keep moments
python tools/evaluate_runs.py template output/<job>/analysis.json research/reference/physics.json
# Accuracy: category agreement, must-keep recall (target 100%) and time removed
python tools/evaluate_runs.py score output/<job>/analysis.json research/reference/physics.json
```

Implementation references: [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/structured-output), [prompt design](https://ai.google.dev/gemini-api/docs/prompting-strategies), and [FFmpeg progress reporting](https://ffmpeg.org/ffmpeg.html). Schema validity and smooth rendering do not establish semantic classification accuracy; evaluate on representative reviewed lectures before broad distribution.
