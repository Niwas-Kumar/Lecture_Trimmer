"""Lecture Trimmer - web UI for teachers.

    python app.py            # opens http://127.0.0.1:7860

Pick a video, choose how much to cut, press one button, download the result.
Which services do the work is decided automatically from the keys in .env
(see README); nothing technical is shown to the user.
"""
import json
import logging
import shutil
import time
import traceback
from pathlib import Path

import gradio as gr

import config
from audio import extract_audio, probe_duration
from classifier import build_units, classify
from cutter import cut_and_join, snap_to_keyframes
from editor import build_keep_list, snap_to_speech
from models import Label, Transcript
from transcriber import auto_backend, transcribe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("trimmer")

MODE_CHOICES = {
    "Remove chatter only  -  keeps all teaching, examples and doubt solving": "balanced",
    "Keep only core content  -  definitions, formulas, solved problems; also cuts Q&A and repetition": "aggressive",
}
LABEL_TITLES = {
    "core_teaching": "Teaching", "worked_example": "Solved examples", "doubt_solving": "Doubt solving",
    "low_density_teaching": "Repetition / filler on topic", "revision_of_previous": "Revision of earlier class",
    "chatter": "Chatter / jokes / greetings", "admin_announcement": "Announcements", "technical_issue": "Technical issues",
    "silence_break": "Silence / breaks",
}
assert set(LABEL_TITLES) == {l.value for l in Label}

CSS = """
.gradio-container {max-width: 1000px !important; margin: auto;}
.step {font-size: 1.05rem; font-weight: 600; margin-top: 12px;}
.hint {color: #666; font-size: 0.9rem;}
"""


# ------------------------------------------------------------------ helpers
def _clean(value) -> str:
    return (value or "").strip()


def _fmt(t: float) -> str:
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {int(t % 60):02d}s"


def _ready() -> str | None:
    """None when everything needed is configured, else a message for the banner."""
    if not shutil.which("ffmpeg"):
        return "This computer is missing a required component (ffmpeg). Please ask whoever set up the tool."
    if not (config.ANTHROPIC_API_KEY or config.GOOGLE_API_KEY):
        return "The tool is not fully set up yet (no AI key found). Please ask whoever set up the tool."
    return None


def _resolve_video(upload, local_path) -> Path:
    local_path = _clean(local_path).strip('"')
    p = Path(local_path) if local_path else (Path(upload) if upload else None)
    if p is None:
        raise gr.Error("Please choose a video first (upload it, or paste its file path).")
    if not p.exists():
        raise gr.Error(f"Could not find this file: {p}")
    return p


def video_info(upload, local_path):
    try:
        video = _resolve_video(upload, local_path)
    except gr.Error:
        return ""
    try:
        duration = probe_duration(video)
    except Exception as e:  # noqa: BLE001
        return f"Could not read this file as a video ({e})."
    cached = (config.TEMP_DIR / f"{video.stem}.transcript.json").exists()
    asr_time = 0 if cached else {"local": duration / 3.5, "groq": 60 + duration / 200, "sarvam": duration / 8}[auto_backend()]
    total = 10 + asr_time + 45 + 10
    note = " (this video was analysed before, so it will be quick)" if cached else ""
    return f"Video length **{_fmt(duration)}**. Estimated time: **about {_fmt(total)}**{note}."


def _get_transcript(video: Path, language, force: bool, progress) -> Transcript:
    cache = config.TEMP_DIR / f"{video.stem}.transcript.json"
    if cache.exists() and not force:
        progress(0.15, desc="Processing…")
        return Transcript.model_validate_json(cache.read_text(encoding="utf-8"))
    progress(0.05, desc="Processing…")
    wav = extract_audio(video)
    progress(0.15, desc="Processing…")
    try:
        tr = transcribe(wav, backend="auto", language=language)
    finally:
        wav.unlink(missing_ok=True)  # the transcript is what we keep, not the audio
    cache.write_text(tr.model_dump_json(indent=1), encoding="utf-8")
    return tr


# ------------------------------------------------------------------ trimming
def run_pipeline(upload, local_path, mode_label, context, language, keep_labels,
                 min_gap, min_island, padding, dry_run, fresh, progress=gr.Progress()):
    try:
        problem = _ready()
        if problem:
            raise gr.Error(problem)
        video = _resolve_video(upload, local_path)
        mode = MODE_CHOICES[mode_label]
        config.MIN_GAP_TO_CUT, config.MIN_KEEP_ISLAND, config.EDGE_PADDING = min_gap, min_island, padding
        keep = {k for k, v in LABEL_TITLES.items() if v in keep_labels}
        t0 = time.perf_counter()

        duration = probe_duration(video)
        transcript = _get_transcript(video, _clean(language) or None, fresh, progress)

        progress(0.55, desc="Processing…")
        units = build_units(transcript, unit_seconds=config.MODES[mode]["unit_seconds"])
        labels = classify(units, context=_clean(context), backend="auto", mode=mode, use_cache=not fresh)
        # Cuts land on sentence boundaries, then on keyframes (so planned length == real length).
        ranges = build_keep_list(units, labels, duration, keep)
        ranges = snap_to_keyframes(snap_to_speech(ranges, transcript), video)

        report = {"video": str(video), "duration": duration, "language": transcript.language, "mode": mode,
                  "units": [{**u.model_dump(), **labels[u.id].model_dump(mode="json", exclude={"unit_id"})} for u in units],
                  "keep": [r.model_dump() for r in ranges]}
        report_path = config.OUTPUT_DIR / f"{video.stem}_labels.json"
        report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")

        out_video = None
        if not dry_run and ranges:
            progress(0.85, desc="Processing…")
            out_video = str(cut_and_join(video, ranges, config.OUTPUT_DIR / f"{video.stem}_trimmed{video.suffix}"))

        # Report the real length of the file we wrote, not the plan.
        kept = probe_duration(Path(out_video)) if out_video else sum(r.end - r.start for r in ranges)
        removed_pct = 100 * (1 - kept / max(duration, 1))
        summary = (f"## ✅ Done in {_fmt(time.perf_counter() - t0)}\n\n"
                   f"**{_fmt(duration)}** → **{_fmt(kept)}**  ({removed_pct:.0f}% removed)"
                   + ("\n\n*Analyse only: no video was written.*" if dry_run else ""))
        progress(1.0, desc="Done")
        return summary, out_video, gr.update(value=out_video, visible=bool(out_video))
    except gr.Error:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        raise gr.Error(f"Something went wrong: {e}") from e


# ------------------------------------------------------------------- layout
def _mode_defaults(mode_label):
    s = config.MODES[MODE_CHOICES[mode_label]]
    return ([LABEL_TITLES[k] for k in LABEL_TITLES if k in s["keep_labels"]],
            s["min_gap_to_cut"], s["min_keep_island"], s["edge_padding"])


with gr.Blocks(title="Lecture Trimmer") as demo:
    gr.Markdown("# 🎬 Lecture Trimmer\n"
                "Turn a long recorded class into a shorter video with only the study material. "
                "Greetings, announcements, jokes, technical problems and revision are cut out automatically.")
    if _ready():
        gr.Markdown(f"> ⚠️ {_ready()}")

    gr.Markdown("### 1. Choose the video", elem_classes="step")
    with gr.Row():
        upload = gr.File(label="Upload video", file_types=["video"], type="filepath", scale=1)
        local_path = gr.Textbox(label="…or paste the file path (recommended for long videos)",
                                placeholder=r"D:\Lectures\physics_class12.mp4", scale=1)
    info = gr.Markdown(elem_classes="hint")

    gr.Markdown("### 2. How much should be cut?", elem_classes="step")
    mode = gr.Radio(list(MODE_CHOICES), value=list(MODE_CHOICES)[0], show_label=False)
    context = gr.Textbox(label="What is this lecture about? (optional, helps accuracy)",
                         placeholder="e.g. Class 12 Physics, Electrostatics")

    gr.Markdown("### 3. Trim", elem_classes="step")
    run_btn = gr.Button("✂️  Trim video", variant="primary", size="lg")

    summary = gr.Markdown()
    download_btn = gr.DownloadButton("⬇️  Download trimmed video", visible=False, variant="primary")
    out_video = gr.Video(label="Preview")

    with gr.Accordion("More options", open=False):
        language = gr.Textbox(label="Language spoken in the class (leave blank to detect automatically)",
                              placeholder="e.g. Hindi, English")
        _m = config.MODES[config.DEFAULT_MODE]
        keep_labels = gr.CheckboxGroup(list(LABEL_TITLES.values()), label="Parts to keep in the video",
                                       value=[LABEL_TITLES[k] for k in LABEL_TITLES if k in _m["keep_labels"]])
        with gr.Row():
            min_gap = gr.Slider(0, 120, value=_m["min_gap_to_cut"], step=1,
                                label="Don't cut out anything shorter than (seconds)", info="fewer, smoother cuts")
            min_island = gr.Slider(0, 120, value=_m["min_keep_island"], step=1,
                                   label="Don't keep isolated bits shorter than (seconds)")
            padding = gr.Slider(0, 5, value=_m["edge_padding"], step=0.5, label="Extra seconds around each cut")
        with gr.Row():
            dry_run = gr.Checkbox(False, label="Analyse only (don't write the video)")
            fresh = gr.Checkbox(False, label="Start fresh (don't reuse the previous analysis of this video)")

    mode.change(_mode_defaults, inputs=mode, outputs=[keep_labels, min_gap, min_island, padding])
    for comp in (upload, local_path):
        comp.change(video_info, inputs=[upload, local_path], outputs=info)
    run_btn.click(run_pipeline,
                  inputs=[upload, local_path, mode, context, language, keep_labels,
                          min_gap, min_island, padding, dry_run, fresh],
                  outputs=[summary, out_video, download_btn])

if __name__ == "__main__":
    try:
        theme = gr.themes.Soft()
    except AttributeError:
        theme = gr.themes.Default()
    demo.launch(inbrowser=True, theme=theme, css=CSS)
