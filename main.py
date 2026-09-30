"""Lecture trimmer: keep only the study material from a long recorded lecture.

    python main.py lecture.mp4                       # local whisper + Claude
    python main.py lecture.mp4 --asr groq            # cloud ASR, much faster on CPU
    python main.py lecture.mp4 --context "Class 11 Physics, Laws of Motion"
    python main.py lecture.mp4 --dry-run             # labels + cut list only, no video

Intermediate results are cached in temp/<video>.transcript.json so you can
re-run the classification with a different prompt or thresholds without
transcribing again.
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import config
from audio import extract_audio, probe_duration
from classifier import build_units, classify
from cutter import cut_and_join, snap_to_keyframes
from editor import build_keep_list, snap_to_speech
from models import Transcript
from transcriber import transcribe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("trimmer")


class Timer:
    def __init__(self):
        self.stages: dict[str, float] = {}
        self._t = time.perf_counter()

    def lap(self, name: str):
        now = time.perf_counter()
        self.stages[name] = now - self._t
        self._t = now
        logger.info("[%s] %.1fs", name, self.stages[name])


def get_transcript(video: Path, backend: str, language: str | None, force: bool) -> Transcript:
    cache = config.TEMP_DIR / f"{video.stem}.transcript.json"
    if cache.exists() and not force:
        logger.info("Using cached transcript %s", cache.name)
        return Transcript.model_validate_json(cache.read_text(encoding="utf-8"))
    wav = extract_audio(video)
    try:
        transcript = transcribe(wav, backend=backend, language=language)
    finally:
        wav.unlink(missing_ok=True)  # the transcript is what we keep, not the audio
    cache.write_text(transcript.model_dump_json(indent=1), encoding="utf-8")
    return transcript


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, help="output video (default: output/<name>_trimmed.mp4)")
    ap.add_argument("--asr", choices=["auto", "local", "groq", "sarvam"], default="auto",
                    help="auto: groq if key set, else sarvam if key set, else local")
    ap.add_argument("--language", help="skip language detection: 'hi'/'en' for local/groq, 'hi-IN'/'en-IN' for sarvam")
    ap.add_argument("--context", default="", help="subject/chapter/topic hint for the classifier")
    ap.add_argument("--classifier", choices=["auto", "claude", "gemini"], default=config.CLASSIFIER_BACKEND,
                    help="LLM that labels the transcript (auto: claude if key set, else gemini)")
    ap.add_argument("--mode", choices=list(config.MODES), default=config.DEFAULT_MODE,
                    help="balanced: keep all teaching; aggressive: only dense core content (60-80%% cut)")
    ap.add_argument("--keep", default=None,
                    help="comma-separated labels to keep (default: the mode's label set)")
    ap.add_argument("--retranscribe", "--fresh", action="store_true",
                    help="ignore cached transcript and cached labels")
    ap.add_argument("--dry-run", action="store_true", help="write labels and cut list, skip the video")
    args = ap.parse_args()

    video: Path = args.video
    if not video.exists():
        logger.error("Video not found: %s", video)
        return 1
    out = args.out or config.OUTPUT_DIR / f"{video.stem}_trimmed{video.suffix}"
    mode = config.MODES[args.mode]
    keep_labels = ({l.strip() for l in args.keep.split(",") if l.strip()} if args.keep
                   else mode["keep_labels"])
    config.MIN_GAP_TO_CUT, config.MIN_KEEP_ISLAND, config.EDGE_PADDING = (
        mode["min_gap_to_cut"], mode["min_keep_island"], mode["edge_padding"])

    timer = Timer()
    duration = probe_duration(video)
    logger.info("Input: %s (%.1f min)", video.name, duration / 60)

    transcript = get_transcript(video, args.asr, args.language, args.retranscribe)
    timer.lap("transcribe")

    units = build_units(transcript, unit_seconds=mode["unit_seconds"])
    labels = classify(units, context=args.context, backend=args.classifier, mode=args.mode,
                      use_cache=not args.retranscribe)
    timer.lap("classify")

    # Cuts land on sentence boundaries, then on keyframes (so planned length == real length).
    ranges = build_keep_list(units, labels, duration, keep_labels)
    ranges = snap_to_keyframes(snap_to_speech(ranges, transcript), video)
    timer.lap("edit-list")

    # Review files: per-unit labels (with reasons) and the final cut list.
    report = {
        "video": str(video), "duration": duration, "language": transcript.language,
        "units": [{**u.model_dump(), **labels[u.id].model_dump(mode="json", exclude={"unit_id"})}
                  for u in units],
        "keep": [r.model_dump() for r in ranges],
    }
    report_path = config.OUTPUT_DIR / f"{video.stem}_labels.json"
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    logger.info("Review file: %s", report_path)

    if not args.dry_run:
        cut_and_join(video, ranges, out)
        timer.lap("cut")

    total = sum(timer.stages.values())
    logger.info("Done in %.1fs  (%s)", total,
                ", ".join(f"{k} {v:.0f}s" for k, v in timer.stages.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
