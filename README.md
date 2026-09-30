# Lecture Trimmer

Turn a 2–4 hour recorded class into a shorter video that contains only the study material.
Greetings, announcements, jokes, technical problems, breaks and revision of earlier classes are
detected from the speech and cut out automatically. Works for any subject and any language,
including Hinglish and other code-mixed Indian classroom speech.

- **One-button web UI** for teachers, plus a CLI for batch use
- **Fast**: the video is never re-encoded; a 2h16m lecture is processed in ~2–3 minutes
- **Cheap**: runs entirely on free tiers (Groq for speech, Gemini for classification), or on Claude for best accuracy
- **Repeatable**: same input → byte-identical output, thanks to cached transcripts and labels

---

## How it works

```
 video ──► ffmpeg: extract audio ──► speech-to-text (timestamps)
                                                │
                                   group into 20–45 s units
                                                │
                       LLM labels every unit in parallel 10-min windows
                       (teaching / example / doubt / chatter / admin / …)
                                                │
                     smoothing: close small gaps, drop tiny islands, pad edges
                                                │
                    snap cuts to sentence boundaries, then to keyframes
                                                │
                  ffmpeg: stream-copy cut + concat (no re-encoding) ──► output
```

| Stage | What happens | Time (2h16m lecture) |
|---|---|---|
| Audio extract | `ffmpeg -vn` to 16 kHz mono; video is not decoded | ~5 s |
| Transcription | Groq `whisper-large-v3-turbo` (10-min chunks in parallel), or Sarvam, or local faster-whisper | ~1 min cloud · ~40 min CPU |
| Classification | Transcript → 20–45 s units → overlapping 10-min windows → LLM returns one schema-enforced label per unit | ~30–60 s (16 calls) |
| Edit list | Keep chosen labels, smooth, snap to speech + keyframes | ~3 s |
| Cut | Per-range stream copy + concat demuxer | ~10 s |

Two trim modes select the prompt and thresholds:

| Mode | Keeps | Unit | Typical result |
|---|---|---|---|
| **balanced** (default) | all teaching, solved examples, doubt solving | 45 s | 10–30 % shorter |
| **aggressive** | only dense definitions, formulas, code, step-by-step solving; also cuts Q&A and repetition | 20 s | 60–80 % shorter |

Labels: `core_teaching`, `worked_example`, `doubt_solving`, `low_density_teaching`,
`revision_of_previous`, `chatter`, `admin_announcement`, `technical_issue`, `silence_break`.

---

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Video / audio | [ffmpeg](https://ffmpeg.org/) 6+ (stream copy, concat demuxer, keyframe index via ffprobe) |
| Speech-to-text | [Groq](https://console.groq.com/) Whisper API · [Sarvam AI](https://www.sarvam.ai/) `saaras` · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (local, CPU/GPU) |
| Classification | [Gemini](https://aistudio.google.com/) via `google-genai` · [Claude](https://console.anthropic.com/) via `anthropic` — both with JSON-schema structured output |
| Data models | pydantic v2 |
| UI | Gradio 5/6 |
| Config | python-dotenv (`.env`) |

Backends are chosen automatically from the keys present in `.env`:

- **Transcription:** Groq → Sarvam → local (first one with a key wins)
- **Classification:** Claude → Gemini

---

## Setup

### 1. Prerequisites

- Python 3.11+
- ffmpeg on `PATH` — Windows: `winget install Gyan.FFmpeg` · macOS: `brew install ffmpeg` · Debian/Ubuntu: `sudo apt install ffmpeg`

### 2. Install

```bash
git clone <this-repo>
cd Lecture_Trimmer
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Keys

```bash
cp .env.example .env
```

Edit `.env`. You need **one** classification key; transcription keys are optional but strongly recommended for speed.

| Key | Purpose | Cost | Get it at |
|---|---|---|---|
| `GOOGLE_API_KEY` | Classification (Gemini) | Free tier | aistudio.google.com |
| `ANTHROPIC_API_KEY` | Classification (Claude, most accurate) | Paid, ~₹30–40 per 4 h lecture | console.anthropic.com |
| `GROQ_API_KEY` | Transcription, fast | Free tier (2 h audio / hour) | console.groq.com |
| `SARVAM_API_KEY` | Transcription, best for Indian languages | Free credits | dashboard.sarvam.ai |

With no transcription key the tool falls back to local faster-whisper on CPU (`small` model, ~20 min per hour of video).
For GPU transcription install the CUDA runtime libraries: `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`.

---

## Usage

### Web UI (for teachers)

```bash
python app.py        # opens http://127.0.0.1:7860
```

1. Choose the video (upload, or paste a local path for long files)
2. Choose *Remove chatter only* or *Keep only core content*; optionally type what the lecture is about
3. Press **Trim video** → download the result

Nothing technical is shown. If keys are missing the page shows a banner asking the user to contact the admin.

### Command line

```bash
python main.py lecture.mp4
python main.py lecture.mp4 --mode aggressive --context "Class 12 Chemistry, Electrochemistry"
python main.py lecture.mp4 --asr sarvam --classifier claude --language hi
python main.py lecture.mp4 --dry-run          # analysis only, no video
python main.py lecture.mp4 --fresh            # ignore cached transcript and labels
python main.py --help
```

### Outputs

| File | Content |
|---|---|
| `output/<name>_trimmed.mp4` | The edited video (same codec and quality as the input) |
| `output/<name>_labels.json` | Every unit with its label, confidence, the model's one-line reason, and the final keep list — for auditing a cut |
| `temp/<name>.transcript.json` | Cached transcript |
| `temp/labels_<hash>.json` | Cached labels per (transcript, mode, context, model) |

---

## Project layout

```
Lecture_Trimmer/
├── app.py            Gradio UI
├── main.py           CLI
├── config.py         paths, keys, models, modes, thresholds
├── models.py         pydantic models: Transcript, Unit, Label, UnitLabel, KeepRange
├── audio.py          ffmpeg audio extraction and chunking
├── transcriber.py    ASR backends: groq / sarvam / local, auto-selection, language codes
├── classifier.py     prompts, unit + window building, Claude/Gemini calls, label cache
├── editor.py         keep-list smoothing, speech-boundary snapping
├── cutter.py         keyframe index, keyframe snapping, stream-copy cut + concat
├── requirements.txt
├── .env.example
└── README.md
```

---

## Configuration (`config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `MODES` | balanced / aggressive | unit length, kept labels and smoothing thresholds per mode |
| `WINDOW_SECONDS` / `WINDOW_OVERLAP_SECONDS` | 600 / 60 | how much transcript each LLM call sees |
| `MIN_GAP_TO_CUT` | 20 s (balanced) | removed stretches shorter than this are kept, avoiding choppy cuts |
| `MIN_KEEP_ISLAND` | 30 s (balanced) | kept stretches shorter than this are dropped as likely mislabels |
| `EDGE_PADDING` | 1.5 s | extra time around each kept range |
| `SPEECH_SNAP_SECONDS` | 3 s | how far a cut may move to land on a sentence boundary |
| `GEMINI_MODEL` / `CLAUDE_MODEL` | `gemini-3.5-flash-lite` / `claude-opus-5-5` | classification models |
| `*_PARALLEL_REQUESTS` | 2–8 | concurrency per API, tuned for free-tier rate limits |

---

## Design notes

- **No re-encoding.** Cuts are made with `ffmpeg -c copy` at keyframes and joined with the concat demuxer, so the cut step takes seconds regardless of length and output quality equals input quality.
- **Reported length = real length.** Ranges are snapped to the actual keyframe index before cutting and the summary measures the produced file.
- **Deterministic.** Transcript and labels are cached by content hash; a repeat run of the same video + mode + context makes zero API calls and produces an identical file. `--fresh` / "Start fresh" clears both.
- **Recall over precision.** Prompts instruct the model to keep teaching when in doubt (balanced) — cutting real teaching is worse than keeping 30 s of chatter. Aggressive mode inverts that trade-off on purpose.
- **Rate limits are handled.** All API backends retry with exponential back-off on 429, and Gemini falls through to a fallback model on 404 / repeated 503.

---

## Limitations

- Cut boundaries are keyframe-aligned (2 s on typical lecture encodes); frame-exact cuts would need re-encoding at the boundaries.
- Local CPU transcription with the `small` model is slow and weaker on Hinglish; use Groq, Sarvam or a GPU for production.
- Free-tier quotas: Groq allows ~2 hours of audio per hour; Gemini ~10–15 requests per minute. The tool backs off rather than failing, so bulk runs are slower but complete.
- The classifier is an LLM: borderline units (teaching vs. repetition) can be judged differently on a *first* run of a new video. Repeats are identical because labels are cached.

## License

MIT
